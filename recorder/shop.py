"""ร้านเช่ากล้อง: profit per rental job + payback of the equipment.

Jobs come from the owner's public queue sheet (Google Sheets, "anyone with the link can view").
  * column A = date (Excel date, or text like 2026/04/04-05 for a 2-day job), B = job name
  * columns C, D, E = camera / lens EF 100-400 / lens RF 70-200 — the rented items are the cells
    filled red (no text), so we download the sheet as .xlsx and read the cell fills.
Money is NOT split into a separate wallet: a job can be linked to items from /money (customer transfer
= revenue, GrabExpress = shipping). Without links we estimate: price table × days, shipping = default.

Data lives on the server only: /var/lib/live-recorder/shop.json (the repo is public).
"""
import io
import json
import logging
import os
import re
import threading
import time
import urllib.request
import zipfile
from datetime import date, datetime, timedelta
from xml.etree import ElementTree as ET

from . import money, settings

log = logging.getLogger("shop")

SHOP_FILE = settings.DATA_DIR / "shop.json"
EQUIP = ["cam", "l100", "l70"]          # order = sheet columns C, D, E
EQUIP_DEFAULT = {"cam": {"name": "กล้อง", "cost": 0}, "l100": {"name": "เลนส์ EF 100-400", "cost": 0},
                 "l70": {"name": "เลนส์ RF 70-200", "cost": 0}}
SHORT = {"cam": "กล้อง", "l100": "100-400", "l70": "70-200"}
GALLERY_URLS = ["https://spongelotus.work/api/gallery", "https://spongelotus.work/photos.json"]
SYNC_EVERY = 15 * 60

_lock = threading.Lock()
_sync_lock = threading.Lock()


def _defaults():
    return {
        "sheet_id": "",
        "ship_default": 200,          # estimated delivery cost (there and back) when nothing is linked
        "actual_from": "",            # jobs from this date on should be linked to /money items
        "equipment": json.loads(json.dumps(EQUIP_DEFAULT)),
        # price per day by combo (keys = sorted equipment ids joined by "+")
        "prices": {"cam": 900, "l100": 550, "l70": 700, "cam+l100": 1400, "cam+l70": 1500},
        "jobs": {},                   # key -> {status, price, ship, links:[money ids], note, items}
        "rows": [],                   # last parsed sheet rows
        "gallery_dates": [],
        "synced_at": 0, "sync_error": "",
    }


def load():
    with _lock:
        return _load_unlocked()


def _load_unlocked():
    if not SHOP_FILE.exists():
        return _defaults()
    try:
        data = json.loads(SHOP_FILE.read_text("utf-8"))
    except Exception:
        log.exception("shop.json unreadable")
        return _defaults()
    base = _defaults()
    for k, v in base.items():
        data.setdefault(k, v)
    for k, v in EQUIP_DEFAULT.items():
        data["equipment"].setdefault(k, dict(v))
    return data


def _write(data):
    settings.DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = SHOP_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), "utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(SHOP_FILE)


def update(fn):
    with _lock:
        data = _load_unlocked()
        result = fn(data)
        _write(data)
    return result


# ---------- reading the queue sheet ----------
def sheet_id_from(text):
    m = re.search(r"/d/([A-Za-z0-9_-]{20,})", text or "")
    if m:
        return m.group(1)
    t = (text or "").strip()
    return t if re.fullmatch(r"[A-Za-z0-9_-]{20,}", t) else ""


def _fetch(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (spongelotus hub)"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}


def _col(ref):
    return re.match(r"[A-Z]+", ref).group()


def parse_xlsx(blob):
    """Return [{date, days, name, items:[equip ids]}] from the first sheet."""
    z = zipfile.ZipFile(io.BytesIO(blob))
    strings = []
    if "xl/sharedStrings.xml" in z.namelist():
        for si in ET.fromstring(z.read("xl/sharedStrings.xml")).findall("m:si", NS):
            strings.append("".join(t.text or "" for t in si.iter("{%s}t" % NS["m"])))
    styles = ET.fromstring(z.read("xl/styles.xml"))
    fills = []
    for f in styles.find("m:fills", NS).findall("m:fill", NS):
        fg = f.find("m:patternFill/m:fgColor", NS)
        fills.append((fg.get("rgb") or "").upper() if fg is not None else "")
    xfs = [int(x.get("fillId", 0)) for x in styles.find("m:cellXfs", NS).findall("m:xf", NS)]

    def is_red(style):
        try:
            rgb = fills[xfs[style]]
        except (IndexError, ValueError):
            return False
        rgb = rgb[-6:]
        if len(rgb) != 6:
            return False
        r, g, b = int(rgb[0:2], 16), int(rgb[2:4], 16), int(rgb[4:6], 16)
        return r >= 200 and g <= 80 and b <= 80

    names = sorted(n for n in z.namelist() if re.match(r"xl/worksheets/sheet\d+\.xml", n))
    sheet = ET.fromstring(z.read(names[0]))
    out = []
    for row in sheet.iter("{%s}row" % NS["m"]):
        cells = {}
        for c in row.findall("m:c", NS):
            v = c.find("m:v", NS)
            val = v.text if v is not None else ""
            if c.get("t") == "s" and val:
                val = strings[int(val)]
            elif c.get("t") == "inlineStr":
                val = "".join(t.text or "" for t in c.iter("{%s}t" % NS["m"]))
            cells[_col(c.get("r"))] = (val or "", int(c.get("s", 0)))
        d, days = parse_date((cells.get("A") or ("", 0))[0])
        name = (cells.get("B") or ("", 0))[0].strip()
        if not d or not name:
            continue
        items = [eq for eq, col in zip(EQUIP, "CDE") if col in cells and is_red(cells[col][1])]
        out.append({"date": d.isoformat(), "days": days, "name": name[:80], "items": items})
    return out


def parse_date(v):
    """Excel serial / '2026/04/04-05' / '2026/12/1' → (date, days)."""
    v = (v or "").strip()
    if not v:
        return None, 1
    if re.fullmatch(r"\d{4,6}(\.0+)?", v):
        try:
            return date(1899, 12, 30) + timedelta(days=int(float(v))), 1
        except Exception:
            return None, 1
    m = re.match(r"(\d{4})[/-](\d{1,2})[/-](\d{1,2})(?:\s*[-–]\s*(\d{1,2}))?", v)
    if not m:
        return None, 1
    try:
        d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None, 1
    days = 1
    if m.group(4):
        end = int(m.group(4))
        days = max(1, end - d.day + 1) if end >= d.day else 1
    return d, days


def keyed(rows):
    """Give every row a stable key: date|name|n (n counts repeats of the same date+name)."""
    seen = {}
    for r in rows:
        base = f'{r["date"]}|{r["name"]}'
        n = seen.get(base, 0)
        seen[base] = n + 1
        r["key"] = base if n == 0 else f"{base}|{n}"
    return rows


def gallery_dates():
    out = set()
    for url in GALLERY_URLS:
        try:
            data = json.loads(_fetch(url, 15))
        except Exception:
            continue
        albums = data.get("albums", []) if isinstance(data, dict) else []
        for a in albums:
            if isinstance(a, dict) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(a.get("date", ""))):
                out.add(a["date"])
    return sorted(out)


def sync(force=False):
    """Download the queue sheet (and gallery dates). Returns error text or ''."""
    d = load()
    if not d["sheet_id"]:
        return "ยังไม่ได้ใส่ลิงก์ชีตคิว"
    if not force and time.time() - d.get("synced_at", 0) < SYNC_EVERY:
        return d.get("sync_error", "")
    if not _sync_lock.acquire(blocking=False):
        return ""
    try:
        err = ""
        rows = None
        try:
            blob = _fetch(f'https://docs.google.com/spreadsheets/d/{d["sheet_id"]}/export?format=xlsx')
            rows = keyed(parse_xlsx(blob))
        except Exception as e:
            log.warning("sheet sync failed: %s", e)
            err = "ดึงชีตคิวไม่ได้ — เช็กว่าชีตแชร์แบบ 'ทุกคนที่มีลิงก์ดูได้'"
        gd = gallery_dates()

        def apply(data):
            if rows is not None:
                data["rows"] = rows
            if gd:
                data["gallery_dates"] = gd
            data["synced_at"] = time.time()
            data["sync_error"] = err
        update(apply)
        return err
    finally:
        _sync_lock.release()


# ---------- maths ----------
def combo(items):
    return "+".join(sorted(items, key=EQUIP.index))


def table_price(d, items):
    """Price per day of a set of items (falls back to summing smaller combos)."""
    if not items:
        return 0
    p = d["prices"]
    c = combo(items)
    if c in p:
        return float(p[c])
    if "cam" in items:  # camera + several lenses: camera combo with the first lens + the other lenses alone
        rest = [x for x in items if x != "cam"]
        return float(p.get(combo(["cam", rest[0]]), p.get("cam", 0))) + sum(float(p.get(x, 0)) for x in rest[1:])
    return sum(float(p.get(x, 0)) for x in items)


def jobs(d, md=None):
    """All jobs with computed numbers, newest first."""
    md = md if md is not None else money.load()
    by_id = {it["id"]: it for it in md.get("items", [])}
    gdates = set(d.get("gallery_dates") or [])
    today = money.now().date().isoformat()
    auto = auto_links(d, md, today)
    out = []
    for r in d["rows"]:
        j = dict(r)
        meta = d["jobs"].get(r["key"], {})
        j["meta"] = meta
        items = meta.get("items") or r["items"]
        j["items"] = items
        status = meta.get("status") or ""
        j["guess_self"] = not status and r["date"] in gdates
        j["status"] = status or "rent"
        j["future"] = r["date"] > today
        j["auto"] = "links" not in meta and r["key"] in auto
        links = [by_id[i] for i in (meta["links"] if "links" in meta else auto.get(r["key"], [])) if i in by_id]
        j["links"] = links
        inc = sum(float(x["amount"]) for x in links if x.get("kind") == "income")
        exp = sum(float(x["amount"]) * (-1 if x.get("kind") == "refund" else 1)
                  for x in links if x.get("kind") in ("expense", "refund"))
        est_price = float(meta["price"]) if meta.get("price") not in (None, "") else table_price(d, items) * r["days"]
        est_ship = float(meta["ship"]) if meta.get("ship") not in (None, "") else float(d["ship_default"])
        j["price_actual"] = inc > 0
        j["ship_actual"] = exp > 0 or meta.get("ship") not in (None, "")
        j["revenue"] = inc if inc > 0 else est_price
        j["ship"] = exp if exp > 0 else est_ship
        if j["status"] == "self":
            j["revenue"], j["ship"] = 0.0, 0.0
        j["profit"] = j["revenue"] - j["ship"]
        j["needs_link"] = (j["status"] == "rent" and not j["future"] and d.get("actual_from")
                           and r["date"] >= d["actual_from"] and inc == 0)
        out.append(j)
    out.sort(key=lambda j: (j["date"], j["key"]), reverse=True)
    return out


def counted(js):
    """Jobs that count toward money: rented out and already happened."""
    return [j for j in js if j["status"] == "rent" and not j["future"]]


def split_by_equipment(d, j):
    """Share a job's profit between its items in proportion to their single-item prices."""
    items = j["items"] or []
    if not items:
        return {}
    w = {x: float(d["prices"].get(x, 0)) or 1.0 for x in items}
    tot = sum(w.values())
    return {x: j["profit"] * w[x] / tot for x in items}


def summary(d, js):
    c = counted(js)
    cost = sum(float(d["equipment"][e].get("cost") or 0) for e in EQUIP)
    profit = sum(j["profit"] for j in c)
    per_eq = {e: 0.0 for e in EQUIP}
    for j in c:
        for e, v in split_by_equipment(d, j).items():
            per_eq[e] += v
    months = {}
    for j in c:
        m = months.setdefault(j["date"][:7], {"n": 0, "rev": 0.0, "ship": 0.0, "profit": 0.0, "est": False})
        m["n"] += 1
        m["rev"] += j["revenue"]
        m["ship"] += j["ship"]
        m["profit"] += j["profit"]
        m["est"] = m["est"] or not (j["price_actual"] and j["ship_actual"])
    # pace = average profit of the last 3 finished months (fall back to whatever exists)
    # pace = average profit of the last 3 calendar months before this one (months with no job count as 0)
    first = money.now().date().replace(day=1)
    done = []
    t = first
    for _ in range(3):
        t = (t - timedelta(days=1)).replace(day=1)
        done.append(t.strftime("%Y-%m"))
    started = min(months) if months else ""
    done = [k for k in done if k >= started]
    pace = sum(months.get(k, {}).get("profit", 0) for k in done) / len(done) if done else 0
    left = max(0.0, cost - profit)
    eta = ""
    if left <= 0 and cost:
        eta = "คืนทุนครบแล้ว 🎉"
    elif pace > 0:
        n = int(-(-left // pace))
        if n > 120:
            n = 0
            eta = "นานกว่า 10 ปี"
        t = money.now().date().replace(day=1)
        for _ in range(n):
            t = (t.replace(day=28) + timedelta(days=4)).replace(day=1)
        if n:
            eta = f"{TH_M[t.month]} {t.year + 543}"
    ships = [j["ship"] for j in c]
    return {"cost": cost, "profit": profit, "left": left, "pct": (profit / cost * 100) if cost else 0,
            "per_eq": per_eq, "months": months, "pace": pace, "eta": eta, "n": len(c),
            "avg_ship": sum(ships) / len(ships) if ships else 0,
            "n_self": sum(1 for j in js if j["status"] == "self" and not j["future"])}


TH_M = ["", "ม.ค.", "ก.พ.", "มี.ค.", "เม.ย.", "พ.ค.", "มิ.ย.", "ก.ค.", "ส.ค.", "ก.ย.", "ต.ค.", "พ.ย.", "ธ.ค."]


def thai_date(iso):
    try:
        x = date.fromisoformat(iso)
        return f"{x.day} {TH_M[x.month]}"
    except Exception:
        return iso


def candidates(j, md, used):
    """Money items near the job date that could belong to it: transfers in + delivery costs."""
    try:
        jd = date.fromisoformat(j["date"])
    except Exception:
        return []
    lo, hi = jd - timedelta(days=10), jd + timedelta(days=j.get("days", 1) + 3)
    out = []
    for it in md.get("items", []):
        try:
            t = datetime.fromisoformat(it["time"][:16]).date()
        except Exception:
            continue
        if not (lo <= t <= hi):
            continue
        k = it.get("kind")
        text = f'{it.get("merchant", "")} {it.get("note", "")} {it.get("category", "")}'.lower()
        ship = k in ("expense", "refund") and ("grab" in text or "messenger" in text or "ส่ง" in text
                                                 or "lalamove" in text or "ไรเดอร์" in text)
        if k == "income" or ship:
            if it["id"] in used and it["id"] not in j["meta"].get("links", []):
                continue
            out.append(it)
    out.sort(key=lambda it: it["time"])
    return out


def used_links(d, except_key=None):
    s = set()
    for k, m in d["jobs"].items():
        if k != except_key:
            s.update(m.get("links", []))
    return s


def suggest(j, cands):
    """Guess which candidate money items belong to the job: a transfer in of the expected price
    (nearest date) and delivery costs within a day of the job."""
    try:
        jd = date.fromisoformat(j["date"])
    except Exception:
        return set()
    want = j["revenue"]
    out = set()
    inc = [it for it in cands if it.get("kind") == "income" and abs(float(it["amount"]) - want) < 0.5]
    if inc:
        best = min(inc, key=lambda it: abs((datetime.fromisoformat(it["time"][:16]).date() - jd).days))
        out.add(best["id"])
    for it in cands:
        if it.get("kind") in ("expense", "refund"):
            t = datetime.fromisoformat(it["time"][:16]).date()
            text = f'{it.get("note", "")}'
            if abs((t - jd).days) <= 1 and ("ส่งให้" in text or "คืนกล้อง" in text or "ลูกค้า" in text):
                out.add(it["id"])
    return out


# ---------- automatic matching (jobs never saved by hand) ----------
SHIP_OUT = ("ส่งให้",)
SHIP_BACK = ("คืนกล้อง", "ส่งถึงบัว", "ส่งคืน")
SHIP_SKIP = ("ส่งของให้ตัวเอง",)


def _is_delivery(it):
    text = f'{it.get("merchant", "")} {it.get("category", "")}'.lower()
    return it.get("kind") in ("expense", "refund") and (
        "messenger" in text or "grabexpress" in text or "lalamove" in text)


def _day(it):
    return datetime.fromisoformat(it["time"][:16]).date()


def auto_links(d, md, today):
    """Match money items to jobs that were never saved by hand (from actual_from on):
    * a transfer in of the expected price → nearest job (customer pays up to ~10 days before, or a few after)
    * GrabExpress "ส่งให้ ..." → the job on that day or within 2 days after (sending the gear out)
    * GrabExpress "ลูกค้าส่งคืนกล้อง" / "ส่งถึงบัว" → the job on that day or up to 3 days before (gear coming back)
    Returns {job key: [money ids]}."""
    start = d.get("actual_from") or ""
    if not start:
        return {}
    taken = set()
    for m in d["jobs"].values():
        taken.update(m.get("links", []))
    open_jobs = []
    for r in d["rows"]:
        meta = d["jobs"].get(r["key"], {})
        if "links" in meta or meta.get("status") == "self" or r["date"] < start or r["date"] > today:
            continue
        items = meta.get("items") or r["items"]
        price = float(meta["price"]) if meta.get("price") not in (None, "") else table_price(d, items) * r["days"]
        open_jobs.append({"key": r["key"], "date": date.fromisoformat(r["date"]), "days": r["days"],
                          "price": price, "out": 0, "back": 0})
    if not open_jobs:
        return {}
    res = {j["key"]: [] for j in open_jobs}
    lo = min(j["date"] for j in open_jobs) - timedelta(days=10)
    pool = []
    for it in md.get("items", []):
        if it["id"] in taken:
            continue
        try:
            t = _day(it)
        except Exception:
            continue
        if t >= lo:
            pool.append((t, it))
    pool.sort(key=lambda x: x[1]["time"])
    # money in: exact price, closest date; each job takes one transfer
    paid = set()
    for j in sorted(open_jobs, key=lambda j: j["date"]):
        best = None
        for t, it in pool:
            if it.get("kind") != "income" or it["id"] in paid or abs(float(it["amount"]) - j["price"]) > 0.5:
                continue
            gap = (t - j["date"]).days
            if -10 <= gap <= j["days"] + 3 and (best is None or abs(gap) < abs(best[0])):
                best = (gap, it)
        if best:
            paid.add(best[1]["id"])
            res[j["key"]].append(best[1]["id"])
            continue
        # paid in two transfers (e.g. 1,400 + 650)
        near = [(t, it) for t, it in pool if it.get("kind") == "income" and it["id"] not in paid
                and -10 <= (t - j["date"]).days <= j["days"] + 3]
        pair = None
        for a_i in range(len(near)):
            for b_i in range(a_i + 1, len(near)):
                x, y = near[a_i], near[b_i]
                if abs(float(x[1]["amount"]) + float(y[1]["amount"]) - j["price"]) <= 0.5:
                    gap = abs((x[0] - j["date"]).days) + abs((y[0] - j["date"]).days)
                    if pair is None or gap < pair[0]:
                        pair = (gap, x[1], y[1])
        if pair:
            for it in pair[1:]:
                paid.add(it["id"])
                res[j["key"]].append(it["id"])
    # deliveries
    for t, it in pool:
        if not _is_delivery(it):
            continue
        note = it.get("note", "")
        if any(x in note for x in SHIP_SKIP):
            continue
        if any(x in note for x in SHIP_BACK):
            fits = [j for j in open_jobs if 0 <= (t - j["date"]).days <= j["days"] + 2]
            side = "back"
        elif any(x in note for x in SHIP_OUT):
            fits = [j for j in open_jobs if 0 <= (j["date"] - t).days <= 2]
            side = "out"
        else:
            continue
        if not fits:
            continue
        # nearest date first, then the job with fewer trips on that side (two jobs on one day)
        j = min(fits, key=lambda j: (abs((t - j["date"]).days), j[side]))
        j[side] += 1
        res[j["key"]].append(it["id"])
    return {k: v for k, v in res.items() if v}
