#!/usr/bin/env python3
"""Lunch menu scraper for petrhrebenar.github.io/papani.

Reads every source in SOURCES, converts what it finds into one JSON file
(menus.json) and saves menu images next to it. Run by
.github/workflows/papani.yml, which publishes the output directory as the
`papani-data` branch.

Adding a restaurant: append an entry to SOURCES. Without a `parser` it is
shown as a plain link. An optional `note` is shown under the name.

transcripts.json in the output directory holds text versions of the menu
images, keyed by image path ("img/<id>-<hash>.<ext>"). It is written by a
separate scheduled task, not by this script; this script only keeps it and
drops entries whose image is gone. A parser receives the page HTML and returns a dict
with any of:
  days:   {"YYYY-MM-DD": [section, ...]}      menu for specific days
  week:   {"from": iso, "to": iso, "sections": [section, ...]}  one menu for the whole week
  images: [{"url": absolute_url, "try": [urls to download from], "date": iso or None}]  menu as a picture
where section = {"name": str, "items": [{"name": str, "price": str}], "extra": bool}.
"""
import argparse
import datetime as dt
import hashlib
import json
import pathlib
import re
import sys
import time
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup

TZ = ZoneInfo("Europe/Prague")
UA = "papani/1.0 (+https://petrhrebenar.github.io/papani/)"
MONTHS = ["ledna", "února", "března", "dubna", "května", "června", "července",
          "srpna", "září", "října", "listopadu", "prosince"]


# ---------- helpers ----------

def clean(s):
    return re.sub(r"\s+", " ", (s or "").replace("\xa0", " ")).strip()


def price(s):
    s = clean(s)
    m = re.search(r"(\d[\d ]*)\s*(?:Kč|,-)", s)
    return f"{m.group(1).replace(' ', '')} Kč" if m else s


def strip_allergens(s):
    # "Fazolová polévka |1,6,9,12|" and the occasional typo "I1,3,7I"
    return clean(re.sub(r"\s*[|I]\s*\d{1,2}(?:\s*,\s*\d{1,2})*\s*[|I]\s*$", "", clean(s)))


def nearest_year(day, month, today):
    """Year that puts day/month closest to today (menus carry no year)."""
    best = None
    for y in (today.year - 1, today.year, today.year + 1):
        try:
            d = dt.date(y, month, day)
        except ValueError:
            continue
        if best is None or abs((d - today).days) < abs((best - today).days):
            best = d
    return best


def fetch(url, binary=False, tries=3):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "cs"})
            with urllib.request.urlopen(req, timeout=30) as r:
                body = r.read()
                if binary:
                    return body, r.headers.get_content_type()
                return body.decode(r.headers.get_content_charset() or "utf-8", "replace")
        except Exception as e:  # noqa: BLE001 - every failure is retried, then reported
            last = e
            time.sleep(2 * (i + 1))
    raise last


# ---------- parsers ----------

def parse_kolkovna(html, ctx):
    """Whole week in HTML: one .op-menu-day block per date."""
    soup = BeautifulSoup(html, "html.parser")
    days = {}
    for day in soup.select(".op-menu-day[data-date]"):
        box = day.select_one(".food-list-daily")
        if not box:
            continue
        soups, mains, label, name = [], [], "", ""
        for node in box.children:
            tag = getattr(node, "name", None)
            if tag == "strong":
                label, name = clean(node.get_text()), ""
            elif tag is None:
                name = clean(name + " " + str(node))
            elif tag == "span" and "price" in (node.get("class") or []):
                if name:
                    item = {"name": strip_allergens(name), "price": price(node.get_text())}
                    (soups if label.lower().startswith("polév") else mains).append(item)
                name = ""
        sections = [s for s in ({"name": "Polévky", "items": soups},
                                {"name": "Hlavní jídla", "items": mains}) if s["items"]]
        if sections:
            days[day["data-date"]] = sections
    return {"days": days}


def parse_castello(html, ctx):
    """One lunch menu for the whole week, with a date range above it."""
    soup = BeautifulSoup(html, "html.parser")
    root = soup.select_one("#menu") or soup
    # The list is rendered twice (an "all" tab and the category tab); the
    # category tab carries the date range, so prefer it.
    desc = root.select_one(".custom-description-section")
    tab = desc.find_parent(class_="tab") if desc else None
    if tab is None:
        tab = root.select_one(".tab") or root
    items = []
    for it in tab.select(".spl-item-root"):
        n, p = it.select_one(".name"), it.select_one(".spl-price")
        if n and clean(n.get_text()):
            items.append({"name": clean(n.get_text()), "price": price(p.get_text()) if p else ""})
    out = {"sections": [{"name": "", "items": items}] if items else []}
    m = re.search(r"(\d{1,2})\.\s*(\d{1,2})\.?\s*[-–]\s*(\d{1,2})\.\s*(\d{1,2})\.?\s*(\d{4})?",
                  clean(desc.get_text()) if desc else "")
    if m:
        d1, m1, d2, m2 = (int(x) for x in m.group(1, 2, 3, 4))
        end = dt.date(int(m.group(5)), m2, d2) if m.group(5) else nearest_year(d2, m2, ctx["today"])
        start = dt.date(end.year if m1 <= m2 else end.year - 1, m1, d1)
        out["from"], out["to"] = start.isoformat(), end.isoformat()
    return {"week": out}


def parse_lokal(html, ctx):
    """Only today's menu is published; the week builds up run by run."""
    soup = BeautifulSoup(html, "html.parser")
    h1 = soup.find("h1", string=re.compile("Denní menu"))
    root = h1.find_parent(["main", "article"]) or soup if h1 else soup
    text = clean(root.get_text(" "))
    m = re.search(r"(?:pondělí|úterý|středa|čtvrtek|pátek|sobota|neděle)\s+(\d{1,2})\.\s*(" + "|".join(MONTHS) + ")",
                  text, re.I)
    if not m:
        raise ValueError("date not found")
    date = nearest_year(int(m.group(1)), MONTHS.index(m.group(2).lower()) + 1, ctx["today"])
    main = {"polévky", "speciality lokálu", "hlavní jídla"}
    sections = []
    for sec in root.find_all("section"):
        h2 = sec.find("h2")
        items = []
        for li in sec.find_all("li"):
            n = li.find(class_=re.compile(r"^_itemName_"))
            p = li.find(class_=re.compile(r"^_price_"))
            if n:
                items.append({"name": clean(n.get_text()), "price": price(p.get_text()) if p else ""})
        if h2 and items:
            name = clean(h2.get_text())
            sections.append({"name": name, "items": items, "extra": name.lower() not in main})
    return {"days": {date.isoformat(): sections}} if sections else {"days": {}}


def parse_lacasablu(html, ctx):
    """Menus are pictures in the homepage slider, named MENU_*."""
    urls = []
    for src in re.findall(r'<img\s[^>]*?\bsrc="([^"]+/wp-content/uploads/[^"]+)"', html):
        src = src.replace("&amp;", "&")
        name = urllib.parse.urlsplit(src).path.rsplit("/", 1)[-1]
        if re.search(r"menu", name, re.I) and src.split("?")[0] not in [u.split("?")[0] for u in urls]:
            urls.append(src)
    images = []
    for u in urls:
        parts = urllib.parse.urlsplit(u)
        m = re.search(r"/uploads/(\d{4})/(\d{2})/", parts.path)
        tries = [u]
        if "wp.com" in parts.netloc:
            # Photon CDN: prefer a sensible width over the 2250 px original,
            # then the image as linked, then the file on the site itself.
            tries = [urllib.parse.urlunsplit(parts._replace(query="w=1400&ssl=1")), u,
                     "https://" + parts.path.lstrip("/")]
        images.append({"url": u, "try": tries, "month": f"{m.group(1)}-{m.group(2)}" if m else None})
    return {"images": images}


def parse_bozskalahvice(html, ctx):
    """Homepage slider: one slide is the cultural programme, the other the menu."""
    images, seen = [], set()
    for src in re.findall(r'["\'(]((?:https?://www\.bozskalahvice\.cz)?/fotky\d+/slider/[^"\')\s]+)', html):
        url = urllib.parse.urljoin(ctx["url"], src)
        name = url.rsplit("/", 1)[-1]
        if url in seen or re.search(r"program", name, re.I):
            continue
        seen.add(url)
        m = re.search(r"_(\d{2})(\d{2})(\d{2})(?:_|\.)", name)  # e.g. ..._261005_2.png
        date = None
        if m:
            try:
                date = dt.date(2000 + int(m.group(1)), int(m.group(2)), int(m.group(3))).isoformat()
            except ValueError:
                pass
        images.append({"url": url, "date": date})
    return {"images": images}


SOURCES = [
    {"id": "kolkovna", "name": "Kolkovna (V Kolkovně)", "url": "https://vkolkovne.kolkovna.cz/", "parser": parse_kolkovna},
    {"id": "castello", "name": "Pizzeria Castello", "url": "https://pizzeriacastello.cz/", "parser": parse_castello},
    {"id": "lokal", "name": "Lokál Dlouhááá", "url": "https://lokal-dlouha.ambi.cz/menu/denni-menu", "parser": parse_lokal,
     "note": "jeden student má slevu 20 % na až 2 jídla"},
    {"id": "lacasablu", "name": "La Casa Blů", "url": "https://lacasablu.cz/", "parser": parse_lacasablu},
    {"id": "bozskalahvice", "name": "Božská lahvice", "url": "https://www.bozskalahvice.cz/", "parser": parse_bozskalahvice},
    # No parser: these sites do not allow automated reading, so they stay links.
    {"id": "kathmandu", "name": "Kathmandu", "url": "https://restauracekathmandu.cz/denni-menu", "parser": None},
    {"id": "uparlamentu", "name": "U Parlamentu", "url": "https://uparlamentu.cz/", "parser": None},
]


# ---------- assembling ----------

def save_images(src, images, out, offline):
    """Download each image into out/img and return entries for menus.json."""
    saved = []
    for im in images:
        entry = {"orig": im["url"], "src": im["url"]}
        for k in ("date", "month"):
            if im.get(k):
                entry[k] = im[k]
        errors = []
        for url in ([] if offline else im.get("try", [im["url"]])):
            try:
                body, ctype = fetch(url, binary=True, tries=2)
                ext = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}.get(ctype)
                if not ext:
                    raise ValueError(f"unexpected content type {ctype}")
                name = f"{src['id']}-{hashlib.sha1(body).hexdigest()[:10]}.{ext}"
                (out / "img").mkdir(parents=True, exist_ok=True)
                (out / "img" / name).write_bytes(body)
                entry["src"] = f"img/{name}"
                break
            except Exception as e:  # noqa: BLE001 - try the next candidate
                errors.append(f"{urllib.parse.urlsplit(url).netloc}: {e}")
        else:
            if errors:  # nothing worked: the page links the restaurant's own URL
                print(f"::warning::{src['id']}: image not saved ({'; '.join(errors)}); linking the original")
        saved.append(entry)
    return saved


def build(today, out, fixtures=None):
    monday = today - dt.timedelta(days=today.weekday())
    sunday = monday + dt.timedelta(days=6)
    in_week = lambda iso: monday.isoformat() <= iso <= sunday.isoformat()  # noqa: E731

    prev = {}
    try:
        old = json.loads((out / "menus.json").read_text(encoding="utf-8"))
        if old.get("week") == monday.isoformat():
            prev = {r["id"]: r for r in old.get("restaurants", [])}
    except (OSError, ValueError):
        pass

    now = dt.datetime.now(TZ).replace(microsecond=0).isoformat()
    restaurants, failed, parsed = [], 0, 0
    for src in SOURCES:
        entry = {"id": src["id"], "name": src["name"], "url": src["url"]}
        if src.get("note"):
            entry["note"] = src["note"]
        if not src["parser"]:
            restaurants.append({**entry, "status": "link"})
            continue
        parsed += 1
        try:
            if fixtures:
                html = (fixtures / f"{src['id']}.html").read_text(encoding="utf-8")
            else:
                html = fetch(src["url"])
            got = src["parser"](html, {"today": today, "url": src["url"]})
        except Exception as e:  # noqa: BLE001 - one broken site must not stop the others
            failed += 1
            print(f"::warning::{src['id']}: {type(e).__name__}: {e}")
            keep = {k: v for k, v in prev.get(src["id"], {}).items() if k in ("days", "week", "images")}
            restaurants.append({**entry, **keep, "status": "error", "error": f"{type(e).__name__}: {e}"[:200]})
            continue

        status = "ok"
        if "days" in got:
            days = {**prev.get(src["id"], {}).get("days", {}), **got["days"]}
            entry["days"] = {d: days[d] for d in sorted(days) if in_week(d)}
            if not entry["days"]:
                status = "stale"
        if "week" in got:
            w = got["week"]
            current = bool(w["sections"]) and ("from" not in w or (w["from"] <= sunday.isoformat() and w["to"] >= monday.isoformat()))
            if current:
                entry["week"] = w
            else:
                status = "stale"
                if w.get("from"):
                    entry["seen"] = {"from": w["from"], "to": w["to"]}
        if "images" in got:
            entry["images"] = save_images(src, got["images"], out, offline=bool(fixtures))
            dates = [i["date"] for i in entry["images"] if i.get("date")]
            last_month = (monday.replace(day=1) - dt.timedelta(days=1)).strftime("%Y-%m")
            months = [i["month"] for i in entry["images"] if i.get("month")]
            if not entry["images"] or (dates and max(dates) < monday.isoformat()) or (months and max(months) < last_month):
                status = "stale"
        restaurants.append({**entry, "status": status})
        print(f"{src['id']}: {status}")

    # drop images no longer referenced
    used = {r_i["src"] for r in restaurants for r_i in r.get("images", [])}
    if (out / "img").is_dir():
        for f in (out / "img").iterdir():
            if f"img/{f.name}" not in used:
                f.unlink()

    tfile = out / "transcripts.json"
    try:
        tr = json.loads(tfile.read_text(encoding="utf-8"))
        kept = {k: v for k, v in tr.items() if k in used}
        if kept != tr:
            tfile.write_text(json.dumps(kept, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    except (OSError, ValueError, AttributeError):
        pass

    data = {"updated": now, "week": monday.isoformat(), "restaurants": restaurants}
    out.mkdir(parents=True, exist_ok=True)
    (out / "menus.json").write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return failed, parsed


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="data", help="output directory (also read for the previous menus.json)")
    ap.add_argument("--fixtures", help="read <id>.html from this directory instead of the network")
    ap.add_argument("--today", help="YYYY-MM-DD, for testing")
    a = ap.parse_args()
    today = dt.date.fromisoformat(a.today) if a.today else dt.datetime.now(TZ).date()
    failed, parsed = build(today, pathlib.Path(a.out), pathlib.Path(a.fixtures) if a.fixtures else None)
    if parsed and failed == parsed:
        sys.exit("every source failed; not publishing")


if __name__ == "__main__":
    main()
