"""Subscription tracker: list of paid services, totals, and ntfy reminders before renewal.

Data lives on the server only (never in this public repo):
  /var/lib/live-recorder/subscriptions.json
"""
import calendar
import json
import logging
import os
import threading
import uuid
from datetime import date, datetime, timedelta

from . import notify, settings

log = logging.getLogger("subs")

SUBS_FILE = settings.DATA_DIR / "subscriptions.json"

CYCLES = {"monthly": "รายเดือน", "yearly": "รายปี", "usage": "จ่ายตามใช้", "free": "ฟรี"}
STATUSES = {"active": "ใช้อยู่", "cancelling": "จะยกเลิก", "cancelled": "ยกเลิกแล้ว"}
CURRENCIES = ("THB", "USD")

_lock = threading.Lock()


def _mock():
    """Starter list (prices/dates are placeholders — edit them in the web page)."""
    today = date.today()

    def d(days):
        return (today + timedelta(days=days)).isoformat()

    rows = [
        ("Claude MAX", 100, "USD", "monthly", d(12), "AI", "active", ""),
        ("IFTTT Pro", 2100, "THB", "yearly", d(40), "Automation", "cancelling",
         "applet YouTube→X พังตั้งแต่ 18 พ.ย. 2026 · จะทำระบบเองแทน"),
        ("โดเมน spongelotus.work", 400, "THB", "yearly", d(150), "เว็บ", "active", "ใช้กับ gallery"),
        ("Oracle Cloud", 0, "THB", "usage", "", "เซิร์ฟเวอร์", "active",
         "Pay As You Go · ผูกบัตรไว้ ต้องคอยเช็กบิล"),
        ("TKREC", 0, "THB", "monthly", "", "อัดไลฟ์", "cancelling",
         "มี live recorder ของตัวเองแล้ว · เช็กว่ายกเลิกหรือยัง"),
        ("Cloudflare", 0, "THB", "free", "", "เว็บ", "active", ""),
        ("GitHub", 0, "THB", "free", "", "เว็บ", "active", ""),
    ]
    items = []
    for name, price, cur, cycle, nxt, cat, status, note in rows:
        items.append({"id": uuid.uuid4().hex[:8], "name": name, "price": price, "currency": cur,
                      "cycle": cycle, "next": nxt, "category": cat, "status": status,
                      "note": note, "mock": True})
    return {"usd_rate": 33.0, "remind_days": 3, "items": items, "reminded": {}}


def load():
    with _lock:
        if not SUBS_FILE.exists():
            data = _mock()
            _write(data)
            return data
        try:
            data = json.loads(SUBS_FILE.read_text("utf-8"))
        except Exception:
            log.exception("subscriptions.json unreadable")
            data = {"items": []}
        data.setdefault("usd_rate", 33.0)
        data.setdefault("remind_days", 3)
        data.setdefault("items", [])
        data.setdefault("reminded", {})
        return data


def _write(data):
    settings.DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = SUBS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(SUBS_FILE)


def save(data):
    with _lock:
        _write(data)


# ---------- helpers ----------
def parse_date(s):
    try:
        return date.fromisoformat(s)
    except Exception:
        return None


def add_months(d, n):
    m = d.month - 1 + n
    y = d.year + m // 12
    m = m % 12 + 1
    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def to_thb(item, rate):
    p = float(item.get("price") or 0)
    return p * rate if item.get("currency") == "USD" else p


def counts(item):
    """Does this item cost money on a schedule?"""
    return item.get("status") != "cancelled" and item.get("cycle") in ("monthly", "yearly")


def totals(data):
    rate = float(data.get("usd_rate") or 33)
    month = year = 0.0
    for it in data["items"]:
        if not counts(it):
            continue
        thb = to_thb(it, rate)
        if it["cycle"] == "monthly":
            month += thb
            year += thb * 12
        else:
            month += thb / 12
            year += thb
    return month, year


def days_left(item, today=None):
    d = parse_date(item.get("next", ""))
    if not d:
        return None
    return (d - (today or date.today())).days


def upcoming(data, within=30):
    out = []
    for it in data["items"]:
        if it.get("status") == "cancelled":
            continue
        n = days_left(it)
        if n is not None and 0 <= n <= within:
            out.append((n, it))
    out.sort(key=lambda x: x[0])
    return out


def fmt_money(v):
    return f"฿{v:,.0f}"


# ---------- daily job ----------
def roll_forward(data, today=None):
    """After a renewal date passes, move it to the next cycle (active items only)."""
    today = today or date.today()
    changed = False
    for it in data["items"]:
        d = parse_date(it.get("next", ""))
        if not d or d >= today or it.get("status") != "active":
            continue
        step = 1 if it.get("cycle") == "monthly" else 12 if it.get("cycle") == "yearly" else 0
        if not step:
            continue
        while d < today:
            d = add_months(d, step)
        it["next"] = d.isoformat()
        changed = True
    return changed


def check_reminders():
    data = load()
    changed = roll_forward(data)
    rate = float(data.get("usd_rate") or 33)
    days = int(data.get("remind_days") or 3)
    url = (settings.load().get("public_url") or "").rstrip("/")
    for it in data["items"]:
        if it.get("status") == "cancelled" or it.get("cycle") == "free":
            continue
        n = days_left(it)
        if n is None or not 0 <= n <= days:
            continue
        key = f'{it["id"]}|{it["next"]}'
        if key in data["reminded"]:
            continue
        when = "วันนี้" if n == 0 else f"อีก {n} วัน"
        price = fmt_money(to_thb(it, rate))
        if it.get("status") == "cancelling":
            title, tag = f"⚠️ {it['name']} จะต่ออายุ{when}", "warning"
            msg = f"ตั้งใจจะยกเลิกไว้ — ถ้ายังไม่ได้ยกเลิก จะโดนตัด {price}"
        else:
            title, tag = f"💳 {it['name']} ตัดบัตร{when}", "credit_card"
            msg = f"{price} ({CYCLES.get(it['cycle'], '')}) · วันที่ {it['next']}"
        if notify.send(title, msg, tags=[tag], click=(url + "/subs") if url else None):
            data["reminded"][key] = datetime.now().isoformat(timespec="seconds")
            changed = True
            log.info("reminder sent: %s", key)
    # forget old reminder marks
    cutoff = (date.today() - timedelta(days=60)).isoformat()
    for k in [k for k in data["reminded"] if k.split("|")[-1] < cutoff]:
        data["reminded"].pop(k)
        changed = True
    if changed:
        save(data)
