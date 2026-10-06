#!/usr/bin/env python3
"""Lunch menu scraper for petrhrebenar.github.io/papani.

Reads every source in SOURCES, converts what it finds into one JSON file
(menus.json) and saves menu images next to it. Run by
.github/workflows/papani.yml, which publishes the output directory as the
`papani-data` branch.

Adding a restaurant: append an entry to SOURCES. Without a `parser` it is
shown as a plain link. An optional `note` is shown under the name. `group`
says in which group of the page the restaurant appears ("faculty" when
omitted; a list puts it in several). When the menu is not in the page
itself, `data_url` names the file to read instead, or `fetch` a function
that returns the text handed to the parser.

transcripts.json in the output directory holds text versions of the menu
images, keyed by image path ("img/<id>-<hash>.<ext>"). It is written by a
separate scheduled task, not by this script; this script only keeps it and
drops entries whose image is gone. A parser receives the page HTML and returns a dict
with any of:
  days:   {"YYYY-MM-DD": [section, ...]}      menu for specific days
  week:   {"from": iso, "to": iso, "sections": [section, ...]}  one menu for the whole week
  images: [{"url": absolute_url, "try": [urls to download from], "date": iso or None}]  menu as a picture
where section = {"name": str, "items": [{"name": str, "price": str, "desc": str}], "extra": bool}.
"""
import argparse
import csv
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
WEEKDAYS = ["pondělí", "úterý", "středa", "čtvrtek", "pátek", "sobota", "neděle"]
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


def fetch(url, binary=False, tries=2, data=None, headers=None):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, data=data, headers={"User-Agent": UA, "Accept-Language": "cs", **(headers or {})})
            with urllib.request.urlopen(req, timeout=20) as r:
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
        # outside lunch hours the page carries no dated menu; keep what earlier runs got
        return {"days": {}}
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


def parse_kathmandu(html, ctx):
    """A standing menu per weekday (no dates) in one table."""
    soup = BeautifulSoup(html, "html.parser")
    table = soup.select_one("table.poledni-menu")
    if table is None:
        raise ValueError("menu table not found")
    monday = ctx["today"] - dt.timedelta(days=ctx["today"].weekday())
    days, date, section = {}, None, None
    for tr in table.select("tbody tr"):
        cells = tr.find_all("td")
        if "cat" in (tr.get("class") or []):
            # "PONDĚLÍ / MONDAY – Polévka / Soup"
            head = clean(tr.get_text(" ")).lower()
            wd = next((i for i, w in enumerate(WEEKDAYS) if head.startswith(w)), None)
            if wd is None:
                date = None
                continue
            date = (monday + dt.timedelta(days=wd)).isoformat()
            section = {"name": "Polévky" if "polévk" in head else "Hlavní jídla", "items": []}
            days.setdefault(date, []).append(section)
        elif date and len(cells) >= 4:
            name = clean(cells[1].get_text(" "))
            prices = [price(c.get_text()) for c in cells[2:4]]
            prices = [x for x in prices if re.search(r"\d", x)]
            if name:
                # two columns: without soup / with soup
                section["items"].append({"name": name, "price": " / ".join(x.replace(" Kč", "") for x in prices) + " Kč" if prices else ""})
    return {"days": {d: [s for s in secs if s["items"]] for d, secs in days.items()}}


def parse_uparlamentu(html, ctx):
    """Week on the homepage: a date range, then per day a soup block and a mains block."""
    soup = BeautifulSoup(html, "html.parser")
    head = soup.select_one("#denni_menu")
    if head is None:
        raise ValueError("menu section not found")
    monday = None
    m = re.search(r"(\d{1,2})\.\s*(?:(\d{1,2})\.)?\s*[-–]\s*(\d{1,2})\.\s*(\d{1,2})\.\s*(\d{4})?", clean(head.get_text(" ")))
    if m:
        d2, m2 = int(m.group(3)), int(m.group(4))
        end = dt.date(int(m.group(5)), m2, d2) if m.group(5) else nearest_year(d2, m2, ctx["today"])
        d1, m1 = int(m.group(1)), int(m.group(2) or m2)
        start = dt.date(end.year if m1 <= m2 else end.year - 1, m1, d1)
        monday = start - dt.timedelta(days=start.weekday())
    if monday is None:
        raise ValueError("date range not found")
    body = head.find_next_sibling("section")
    if body is None:
        raise ValueError("menu body not found")
    days, date, groups = {}, None, []
    for node in body.find_all(["h3", "p"]):
        if node.name == "h3":
            wd = clean(node.get_text()).lower()
            date = (monday + dt.timedelta(days=WEEKDAYS.index(wd))).isoformat() if wd in WEEKDAYS else None
            groups = []
            continue
        m = re.match(r"^(.*?)\s*(\d{2,4})\s*(?:,-|Kč)\s*$", clean(node.get_text(" ")))
        if not (date and m):
            continue
        # the first block of lines under a day is the soup, the rest are mains
        if node.parent not in groups:
            groups.append(node.parent)
        name = "Polévky" if groups.index(node.parent) == 0 and len(groups) == 1 else "Hlavní jídla"
        secs = days.setdefault(date, [])
        if not secs or secs[-1]["name"] != name:
            secs.append({"name": name, "items": []})
        secs[-1]["items"].append({"name": m.group(1), "price": f"{m.group(2)} Kč"})
    return {"days": days}


def parse_menubot(text, ctx):
    """menubot.cz export used by the restaurant's own page: a TSV with one
    dish per line and the date it is for. Only today's menu is published."""
    days = {}
    for row in csv.DictReader(text.lstrip("\ufeff").splitlines(), delimiter="\t"):
        row = {clean(k): clean(v) for k, v in row.items() if k}
        name, date, cat = row.get("název"), row.get("date"), row.get("kategorie", "")
        if not name or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date or ""):
            continue
        # "1. LEDVINKY NA CIBULCE" -> "Ledvinky na cibulce"
        name = re.sub(r"^\d+\.\s*", "", name)
        name = name[:1].upper() + name[1:].lower()
        section = cat[:1].upper() + cat[1:].lower()
        secs = days.setdefault(date, [])
        sec = next((x for x in secs if x["name"] == section), None)
        if sec is None:
            sec = {"name": section, "items": [], "extra": section not in ("Polévky", "Hlavní jídla")}
            secs.append(sec)
        item = {"name": name, "price": price(row.get("cena", ""))}
        if row.get("popis"):
            item["desc"] = row["popis"]
        sec["items"].append(item)
    return {"days": days}


def parse_untappd(text, ctx):
    """Untappd for Business embed: a script that writes the menu HTML into
    the page. Only today's menu is published, dated in its title."""
    m = re.search(r'container\.innerHTML = ("(?:[^"\\]|\\.)*");', text)
    if not m:
        raise ValueError("embedded menu not found")
    soup = BeautifulSoup(json.loads(m.group(1).replace("\\'", "'")), "html.parser")
    days = {}
    for tab in soup.select(".tab-content") or [soup]:
        title = tab.select_one(".menu-title")
        d = re.search(r"(\d{1,2})\.\s*(\d{1,2})\.", clean(title.get_text()) if title else "")
        if not d:
            continue
        date = nearest_year(int(d.group(1)), int(d.group(2)), ctx["today"]).isoformat()
        for sec in tab.select(".section"):
            head = sec.select_one(".section-name")
            name = clean(head.get_text()) if head else ""
            name = "Polévky" if name.lower().startswith("polév") else name
            items = []
            for it in sec.select(".menu-item"):
                n, desc, pr = it.select_one(".item-name"), it.select_one(".item-description p"), it.select_one(".price")
                if not n:
                    continue
                # the description continues the name; allergens ("A: 1,3,7") go
                full = clean(n.get_text()) + " " + (clean(desc.get_text()) if desc else "")
                full = clean(re.sub(r"\s*A\s*:\s*[\d\s,]+$", "", clean(full)))
                num = re.search(r"\d+(?:[.,]\d+)?", clean(pr.get_text()) if pr else "")
                cost = ""
                if num:
                    v = float(num.group(0).replace(",", "."))
                    cost = (str(int(v)) if v.is_integer() else f"{v:.2f}".replace(".", ",")) + " Kč"
                items.append({"name": full, "price": cost})
            if items:
                days.setdefault(date, []).append({"name": name, "items": items})
    return {"days": days}


MENSA_API = "https://kam-septim-fe.is.cuni.cz/webcarecanteenapiweb/"


def fetch_mensa(ctx):
    """The canteen page is an app; its public menu comes from a JSON API
    that wants an anonymous session token first."""
    monday = ctx["today"] - dt.timedelta(days=ctx["today"].weekday())
    js = {"Content-Type": "application/json"}
    token = json.loads(fetch(MENSA_API + "core/1.0/sessionStart", data=b"{}", headers=js))["sessionToken"]
    js["X-SESSION-TOKEN"] = token
    fetch(MENSA_API + "core/1.0/languageSet", data=b'{"language":"cs"}', headers=js)
    return fetch(MENSA_API + f"canteen/1.0/menuBoard?canteenIds={ctx['canteen']}"
                 f"&from={monday.isoformat()}&to={(monday + dt.timedelta(days=4)).isoformat()}",
                 headers={"X-SESSION-TOKEN": token})


def parse_mensa(text, ctx):
    """menuBoard JSON: one entry per dish with the standard and the student price."""
    def kc(x):
        return str(int(x)) if float(x).is_integer() else f"{x:.2f}".replace(".", ",")

    days = {}
    for board in json.loads(text):
        for m in board.get("menu", []):
            name = clean((m.get("product") or {}).get("name"))
            if not name or not m.get("date"):
                continue
            # "Menu 3 - Vegetarián" -> "(vegetarián)"
            kind = clean(m.get("name", "")).partition(" - ")[2]
            if kind:
                name += f" ({kind.lower()})"
            prices = [m.get("price")] + [o.get("price") for o in m.get("otherPrices", []) if o.get("categoryKey") == "student"]
            prices = [kc(x) for x in prices if isinstance(x, (int, float))]
            if len(set(prices)) == 1:
                prices = prices[:1]
            section = "Polévky" if clean(m.get("group", "")).lower().startswith("polév") else "Hlavní jídla"
            secs = days.setdefault(m["date"], [])
            sec = next((x for x in secs if x["name"] == section), None)
            if sec is None:
                sec = {"name": section, "items": []}
                secs.append(sec)
            sec["items"].append({"name": name, "price": " / ".join(prices) + " Kč" if prices else ""})
    for secs in days.values():
        secs.sort(key=lambda x: x["name"] != "Polévky")
    return {"days": days}


SOURCES = [
    {"id": "kolkovna", "name": "Kolkovna (V Kolkovně)", "url": "https://vkolkovne.kolkovna.cz/", "parser": parse_kolkovna},
    {"id": "castello", "name": "Pizzeria Castello", "url": "https://pizzeriacastello.cz/", "parser": parse_castello},
    {"id": "lokal", "name": "Lokál Dlouhááá", "url": "https://lokal-dlouha.ambi.cz/menu/denni-menu", "parser": parse_lokal,
     "note": "jeden student má slevu 20 % na až 2 jídla"},
    {"id": "lacasablu", "name": "La Casa Blů", "url": "https://lacasablu.cz/", "parser": parse_lacasablu},
    {"id": "bozskalahvice", "name": "Božská lahvice", "url": "https://www.bozskalahvice.cz/", "parser": parse_bozskalahvice},
    {"id": "kathmandu", "name": "Kathmandu", "url": "https://restauracekathmandu.cz/denni-menu", "parser": parse_kathmandu,
     "note": "ceny bez polévky / s polévkou", "group": ["faculty", "nedochvilni"]},
    {"id": "uparlamentu", "name": "U Parlamentu", "url": "https://uparlamentu.cz/", "parser": parse_uparlamentu},
    {"id": "mensa", "name": "Menza Právnická", "url": "https://kam-septim-fe.is.cuni.cz/menu?canteen=11&view=week",
     "fetch": fetch_mensa, "canteen": 11, "parser": parse_mensa, "note": "ceny běžná / studenti"},
    {"id": "vinohradskyparlament", "name": "Vinohradský parlament", "url": "https://www.vinohradskyparlament.cz/",
     "data_url": "https://www.menubot.cz/app/users/Access-Control-Allow-Origin.php?hash=vinohradskyparlament264125698&file=shoptet.tsv",
     "parser": parse_menubot, "group": "nedochvilni"},
    {"id": "zapomenutycas", "name": "Zapomenutý čas", "url": "https://www.zapomenutycas.cz/",
     "data_url": "https://business.untappd.com/locations/32669/themes/126205/js",
     "parser": parse_untappd, "group": "nedochvilni"},
    # same site template as Kathmandu
    {"id": "everest", "name": "Everest", "url": "https://www.restauraceeverest.cz/denni-menu", "parser": parse_kathmandu,
     "note": "ceny bez polévky / s polévkou", "group": "nedochvilni"},
    # An entry with "parser": None is shown as a plain link.
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
        entry = {"id": src["id"], "name": src["name"], "url": src["url"], "group": src.get("group", "faculty")}
        if src.get("note"):
            entry["note"] = src["note"]
        if not src["parser"]:
            restaurants.append({**entry, "status": "link"})
            continue
        parsed += 1
        try:
            ctx = {"today": today, "url": src["url"], "canteen": src.get("canteen")}
            if fixtures:
                html = (fixtures / f"{src['id']}.html").read_text(encoding="utf-8")
            elif src.get("fetch"):
                html = src["fetch"](ctx)
            else:
                html = fetch(src.get("data_url") or src["url"])
            got = src["parser"](html, ctx)
        except Exception as e:  # noqa: BLE001 - one broken site must not stop the others
            failed += 1
            print(f"::warning::{src['id']}: {type(e).__name__}: {e}")
            keep = {k: v for k, v in prev.get(src["id"], {}).items() if k in ("days", "week", "images", "checked")}
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
        restaurants.append({**entry, "status": status, "checked": now})
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
