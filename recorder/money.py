"""รายรับรายจ่าย: bank alerts (KBank SMS / LINE KBank Live) arrive from the phone via MacroDroid,
get parsed into transactions, and are shown per wallet (KBank · TrueMoney · เงินสด).

Phone → POST /money/in?k=<token>  (body = notification text)
  * OTP messages are dropped and never stored.
  * Messages we can't read go to an inbox ("อ่านไม่ออก") so the format can be added later.
  * Account numbers are masked (only the last 4 digits are kept).

Data lives on the server only: /var/lib/live-recorder/money.json
"""
import hashlib
import json
import logging
import os
import re
import secrets
import threading
from datetime import datetime, timedelta

from . import record, settings

log = logging.getLogger("money")

MONEY_FILE = settings.DATA_DIR / "money.json"

WALLETS = {"kbank": "KBank", "truemoney": "TrueMoney", "cash": "เงินสด"}
KINDS = {"expense": "รายจ่าย", "income": "รายรับ", "transfer": "ย้ายเงิน", "refund": "คืนเงิน"}
EXPENSE_CATS = ["อาหาร", "เดินทาง", "ช้อปปิ้ง", "บิล/ค่าบริการ", "สุขภาพ", "บันเทิง", "อื่นๆ"]
INCOME_CATS = ["เงินเดือน", "โอนเข้า", "รายได้อื่น"]

# keyword (matched case-insensitively against merchant / text) → category
DEFAULT_RULES = [
    ("ANTHROPIC", "บิล/ค่าบริการ"), ("CLAUDE", "บิล/ค่าบริการ"), ("ORACLE", "บิล/ค่าบริการ"),
    ("GOOGLE", "บิล/ค่าบริการ"), ("APPLE.COM", "บิล/ค่าบริการ"), ("NETFLIX", "บิล/ค่าบริการ"),
    ("SPOTIFY", "บิล/ค่าบริการ"), ("YOUTUBE", "บิล/ค่าบริการ"), ("CLOUDFLARE", "บิล/ค่าบริการ"),
    ("IFTTT", "บิล/ค่าบริการ"), ("TRUE MOVE", "บิล/ค่าบริการ"), ("AIS", "บิล/ค่าบริการ"),
    ("BTS", "เดินทาง"), ("MRT", "เดินทาง"), ("RABBIT", "เดินทาง"), ("LINE PAY", "เดินทาง"),
    ("LINEPAY", "เดินทาง"), ("BOLT", "เดินทาง"), ("PTT", "เดินทาง"),
    ("7-ELEVEN", "อาหาร"), ("7ELEVEN", "อาหาร"), ("เซเว่น", "อาหาร"), ("GRABFOOD", "อาหาร"),
    ("LINEMAN", "อาหาร"), ("FOODPANDA", "อาหาร"), ("STARBUCKS", "อาหาร"), ("CAFE AMAZON", "อาหาร"),
    ("GRAB", "เดินทาง"),
    ("SHOPEE", "ช้อปปิ้ง"), ("LAZADA", "ช้อปปิ้ง"), ("UNIQLO", "ช้อปปิ้ง"), ("WATSON", "ช้อปปิ้ง"),
    ("BOOTS", "ช้อปปิ้ง"), ("IKEA", "ช้อปปิ้ง"),
    ("STRAVA", "สุขภาพ"), ("HOSPITAL", "สุขภาพ"), ("โรงพยาบาล", "สุขภาพ"), ("PHARMACY", "สุขภาพ"),
    ("STEAM", "บันเทิง"), ("MAJOR", "บันเทิง"), ("SF CINEMA", "บันเทิง"),
]

# money moved to another of her wallets (not spending)
TO_WALLET = [("TRUEMONEY", "truemoney"), ("TRUE MONEY", "truemoney"), ("ทรูมันนี่", "truemoney"),
             ("WALLET", "truemoney"), ("ATM", "cash"), ("ถอนเงินสด", "cash"), ("กดเงิน", "cash")]

_lock = threading.Lock()


def now():
    return datetime.now(record.TZ)


def _defaults():
    return {
        "token": secrets.token_hex(16),
        "wallets": {k: {"opening": 0.0} for k in WALLETS},
        "expense_cats": list(EXPENSE_CATS),
        "income_cats": list(INCOME_CATS),
        "rules": [{"kw": k, "cat": c} for k, c in DEFAULT_RULES],
        "items": [],
        "inbox": [],       # messages we couldn't read (masked)
        "seen": {},        # dedupe key -> iso time
        "last_in": "",     # last time the phone sent anything
        "bank_balance": None, "bank_balance_at": "",
    }


def load():
    with _lock:
        if not MONEY_FILE.exists():
            data = _defaults()
            _write(data)
            return data
        return load_unlocked()


def _write(data):
    settings.DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = MONEY_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), "utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(MONEY_FILE)


def save(data):
    with _lock:
        _write(data)


def update(fn):
    """Load → change → save under the lock. Returns whatever fn returns."""
    with _lock:
        data = load_unlocked()
        result = fn(data)
        _write(data)
    return result


# ---------- text helpers ----------
def mask(text):
    """Keep only the last 4 digits of anything that looks like an account / card number."""
    text = re.sub(r"\d[\d\-x ]{6,}\d", lambda m: "x-" + re.sub(r"\D", "", m.group())[-4:]
                  if len(re.sub(r"\D", "", m.group())) >= 8 and "." not in m.group() else m.group(), text)
    return text


def norm(text):
    return re.sub(r"\s+", " ", (text or "").replace("​", "")).strip()


def num(s):
    try:
        return float(s.replace(",", ""))
    except Exception:
        return None


AMT = r"([\d,]+\.\d{2})"
OTP_RE = re.compile(r"\bOTP\b|รหัส\s*OTP|รหัสยืนยัน|one[- ]time|verification code|รหัสผ่านครั้งเดียว", re.I)


def is_otp(text):
    return bool(OTP_RE.search(text or ""))


def classify(data, merchant_or_text):
    t = (merchant_or_text or "").upper()
    for r in data.get("rules", []):
        kw = (r.get("kw") or "").strip().upper()
        if kw and kw in t:
            return r.get("cat", "")
    return ""


def to_wallet(text):
    t = (text or "").upper()
    for kw, w in TO_WALLET:
        if kw.upper() in t:
            return w
    return ""


def _when(hhmm, received):
    """Combine the HH:MM in the message with the day it arrived."""
    if not hhmm:
        return received
    try:
        h, m = map(int, hhmm.split(":"))
        d = received.replace(hour=h, minute=m, second=0, microsecond=0)
        if d - received > timedelta(hours=1):   # e.g. 23:58 message delivered after midnight
            d -= timedelta(days=1)
        return d
    except Exception:
        return received


def parse(text, received=None):
    """Read one KBank alert. Returns dict or None.
    dict: kind, amount, merchant, time(datetime), balance, card(last4), refund(bool)"""
    received = received or now()
    t = norm(text)
    if not t or is_otp(t):
        return None

    # 1) KBank card SMS:  ชำระ 3,522.58บ บัตร x-5676@ANTHROPIC* CLAUDE SUB 18:47น เหลือ 434.76บ
    m = re.search(r"(ชำระ|คืนเงิน|ยกเลิก)\s*" + AMT + r"\s*(?:บ|บาท)\s*บัตร\s*x\s*-?\s*(\d{4})\s*@\s*(.+?)\s+"
                  r"(\d{1,2}:\d{2})\s*น\.?(?:\s*เหลือ\s*" + AMT + r"\s*(?:บ|บาท))?", t)
    if m:
        refund = m.group(1) != "ชำระ"
        return {"kind": "refund" if refund else "expense", "amount": num(m.group(2)),
                "merchant": norm(m.group(4)).rstrip("*").strip(), "card": m.group(3),
                "time": _when(m.group(5), received), "balance": num(m.group(6)) if m.group(6) else None}

    # 2) anything else: decide direction from words, take the first amount that isn't the balance
    bal = None
    mb = re.search(r"(?:คงเหลือ|เหลือ|ยอดเงินคงเหลือ|Bal(?:ance)?\.?)\s*:?\s*" + AMT, t, re.I)
    if mb:
        bal = num(mb.group(1))
    rest = t[:mb.start()] + " " + t[mb.end():] if mb else t
    amounts = re.findall(r"[-+]?\s*" + AMT, rest)
    if not amounts:
        return None
    amt_txt = amounts[0]
    amount = num(re.sub(r"[^\d.,]", "", amt_txt))
    if not amount:
        return None
    IN = r"เงินเข้า|รับโอน|รับเงิน|โอนเข้า|ฝากเงิน|ฝาก|ได้รับ|เข้าบัญชี|received|deposit"
    OUT = r"เงินออก|โอน/ถอน|โอนเงิน|ถอน|โอนออก|จ่าย|ชำระ|ซื้อ|หักบัญชี|จากบัญชี|ตัดบัญชี|withdraw|transfer|payment|paid"
    if amt_txt.strip().startswith("-"):
        kind = "expense"
    elif amt_txt.strip().startswith("+"):
        kind = "income"
    elif re.search(IN, t, re.I):
        kind = "income"
    elif re.search(OUT, t, re.I):
        kind = "expense"
    else:
        return None
    hh = re.search(r"(\d{1,2}:\d{2})\s*น", t)
    merchant = ""
    mm = re.search(r"(?:ไปยัง|ให้กับ|ให้|ที่|@|จาก|from|to)\s*([^\d][^,:]{1,40}?)(?=\s+\d|\s*$|\s*คงเหลือ|\s*เหลือ)", rest)
    if mm and "บัญชี" not in mm.group(1):
        merchant = norm(mm.group(1))
    return {"kind": kind, "amount": amount, "merchant": merchant, "card": "",
            "time": _when(hh.group(1), received) if hh else received, "balance": bal}


# ---------- incoming from the phone ----------
def _key(p):
    if p.get("balance") is not None:
        return f'{p["kind"]}|{p["amount"]:.2f}|{p["balance"]:.2f}'
    return f'{p["kind"]}|{p["amount"]:.2f}|{p.get("merchant", "")}|{p["time"].strftime("%Y-%m-%d %H:%M")}'


def is_bank(title, text):
    """Only KBank messages are read; anything else from the phone is dropped unseen."""
    if "KBANK" in (title or "").upper().replace(" ", ""):
        return True
    return bool(re.search(r"(ชำระ|คืนเงิน)\s*[\d,]+\.\d{2}\s*บ\s*บัตร\s*x", norm(text)))


def ingest(text, source="", received=None, title=None):
    """Store one alert. Returns (status, item_or_none).
    status: added | duplicate | otp | unread | ignored | empty"""
    received = received or now()
    text = (text or "")[:2000]
    if not norm(text) or (title is not None and not is_bank(title, text)):
        # still note that the phone reached us (shows "connected" on the page); content is not kept
        update(lambda d: d.update(last_in=received.isoformat(timespec="seconds")))
        return ("empty" if not norm(text) else "ignored"), None
    if is_otp(text):
        log.info("dropped an OTP message")
        update(lambda d: d.update(last_in=received.isoformat(timespec="seconds")))
        return "otp", None
    p = parse(text, received)

    def fn(d):
        d["last_in"] = received.isoformat(timespec="seconds")
        # forget dedupe keys older than 3 days
        cutoff = (received - timedelta(days=3)).isoformat()
        d["seen"] = {k: v for k, v in d["seen"].items() if v >= cutoff}
        if not p:
            h = hashlib.sha1(norm(text).encode()).hexdigest()[:10]
            if any(x.get("h") == h for x in d["inbox"]):
                return "duplicate", None
            d["inbox"].insert(0, {"id": secrets.token_hex(4), "h": h, "text": mask(norm(text))[:500],
                                  "source": source[:40], "at": received.isoformat(timespec="seconds")})
            d["inbox"] = d["inbox"][:50]
            return "unread", None
        key = _key(p)
        if key in d["seen"]:
            return "duplicate", None
        d["seen"][key] = received.isoformat(timespec="seconds")
        item = build_item(d, p, source, text)
        d["items"].insert(0, item)
        if p.get("balance") is not None:
            d["bank_balance"] = p["balance"]
            d["bank_balance_at"] = p["time"].isoformat(timespec="minutes")
            # keep the KBank wallet equal to what the bank says
            others = sum(signed(it, "kbank") for it in d["items"])
            d["wallets"]["kbank"]["opening"] = round(p["balance"] - others, 2)
        return "added", item

    with _lock:
        d = load_unlocked()
        res = fn(d)
        _write(d)
    log.info("alert from %s: %s", source or "?", res[0])
    return res


def load_unlocked():
    if not MONEY_FILE.exists():
        return _defaults()
    try:
        data = json.loads(MONEY_FILE.read_text("utf-8"))
    except Exception:
        log.exception("money.json unreadable — keeping a copy")
        try:
            MONEY_FILE.replace(MONEY_FILE.with_name(f"money.broken-{int(now().timestamp())}.json"))
        except Exception:
            pass
        data = {}
    for k, v in _defaults().items():
        data.setdefault(k, v)
    for k in WALLETS:
        data["wallets"].setdefault(k, {"opening": 0.0})
    return data


def build_item(d, p, source, text):
    merchant = p.get("merchant", "")
    item = {"id": secrets.token_hex(4), "time": p["time"].isoformat(timespec="minutes"),
            "kind": p["kind"], "amount": round(p["amount"], 2), "wallet": "kbank", "to": "",
            "category": "", "merchant": merchant[:60], "note": "", "auto": True,
            "source": source[:40], "raw": mask(norm(text))[:300], "review": False}
    hint = merchant + " " + norm(text)
    if p["kind"] == "expense":
        w = to_wallet(hint)
        if w and w != "kbank":
            item.update(kind="transfer", to=w)
        else:
            item["category"] = classify(d, hint)
    elif p["kind"] == "refund":
        # same category as the matching charge
        match = next((x for x in d["items"] if x["kind"] == "expense" and abs(x["amount"] - p["amount"]) < 0.01
                      and x.get("merchant") == merchant), None)
        item["category"] = match["category"] if match else classify(d, hint)
    elif p["kind"] == "income":
        item["category"] = "โอนเข้า"
    item["review"] = not item["category"] and item["kind"] in ("expense", "refund")
    return item


# ---------- money maths ----------
def signed(it, wallet):
    """Effect of one item on one wallet."""
    a = float(it.get("amount") or 0)
    k = it.get("kind")
    eff = 0.0
    if it.get("wallet") == wallet:
        eff += a if k in ("income", "refund") else -a
    if k == "transfer" and it.get("to") == wallet:
        eff += a
    return eff


def balances(d):
    out = {}
    for w in WALLETS:
        out[w] = float(d["wallets"][w].get("opening") or 0) + sum(signed(it, w) for it in d["items"])
    return out


def month_key(it):
    return (it.get("time") or "")[:7]


def month_summary(d, ym):
    exp = inc = 0.0
    cats = {}
    for it in d["items"]:
        if month_key(it) != ym:
            continue
        a = float(it.get("amount") or 0)
        if it["kind"] == "expense":
            exp += a
            c = it.get("category") or "ยังไม่ใส่หมวด"
            cats[c] = cats.get(c, 0) + a
        elif it["kind"] == "refund":
            exp -= a
            c = it.get("category") or "ยังไม่ใส่หมวด"
            cats[c] = cats.get(c, 0) - a
        elif it["kind"] == "income":
            inc += a
    cats = sorted(((c, v) for c, v in cats.items() if abs(v) >= 0.005), key=lambda x: -x[1])
    return exp, inc, cats


def fmt(v, sign=False):
    s = f"฿{abs(v):,.2f}".replace(".00", "")
    if sign:
        return ("-" if v < 0 else "+") + s
    return ("-" if v < 0 else "") + s


def needs_review(d):
    return [it for it in d["items"] if it.get("review")]


def learn(d, merchant, cat):
    """Remember merchant → category (used by 'จำร้านนี้')."""
    kw = (merchant or "").strip().upper()[:40]
    if not kw or not cat:
        return
    d["rules"] = [r for r in d["rules"] if r.get("kw", "").upper() != kw]
    d["rules"].insert(0, {"kw": kw, "cat": cat})
    for it in d["items"]:
        if it.get("review") and (it.get("merchant") or "").upper() == kw:
            it["category"] = cat
            it["review"] = False


# ---------- bulk import (statement converted to CSV) ----------
IMPORT_COLS = ["time", "kind", "amount", "wallet", "to", "category", "merchant", "note", "balance"]


def import_csv(d, text):
    """Add rows from CSV text: time,kind,amount,wallet,to,category,merchant,note
    time = YYYY-MM-DD HH:MM · kind = expense/income/transfer/refund · skips rows already there.
    Returns (added, skipped)."""
    import csv
    import io
    # same minute + type + amount + shop = already there (two different shops in one minute are both kept)
    have = {(it.get("time", "")[:16], it.get("kind"), round(float(it.get("amount") or 0), 2), it.get("merchant", ""))
            for it in d["items"]}
    added = skipped = 0
    latest = None   # (time, KBank balance after that row) — from bank statements
    for row in csv.reader(io.StringIO(text.strip())):
        if not row or row[0].strip().lower() in ("time", "เวลา"):
            continue
        row = [c.strip() for c in row] + [""] * (len(IMPORT_COLS) - len(row))
        r = dict(zip(IMPORT_COLS, row))
        try:
            t = datetime.strptime(r["time"].replace("T", " ")[:16], "%Y-%m-%d %H:%M").strftime("%Y-%m-%dT%H:%M")
            amount = round(abs(float(r["amount"].replace(",", ""))), 2)
        except ValueError:
            skipped += 1
            continue
        kind = r["kind"] if r["kind"] in KINDS else "expense"
        wallet = r["wallet"] if r["wallet"] in WALLETS else "kbank"
        to = r["to"] if kind == "transfer" and r["to"] in WALLETS else ""
        key = (t, kind, amount, r["merchant"][:60])
        try:
            b = float(r["balance"].replace(",", "")) if r["balance"] else None
        except ValueError:
            b = None
        if b is not None and wallet == "kbank" and (latest is None or t >= latest[0]):
            latest = (t, b)
        if not amount or key in have:
            skipped += 1
            continue
        have.add(key)
        cat = r["category"][:40] if kind != "transfer" else ""
        if not cat and kind in ("expense", "refund"):
            cat = classify(d, r["merchant"] + " " + r["note"])
        if not cat and kind == "income":
            cat = "โอนเข้า"
        d["items"].append({"id": secrets.token_hex(4), "time": t, "kind": kind, "amount": amount,
                           "wallet": wallet, "to": to, "category": cat, "merchant": r["merchant"][:60],
                           "note": r["note"][:200], "auto": False, "source": "นำเข้า", "raw": "",
                           "review": False})
        added += 1
    d["items"].sort(key=lambda it: it.get("time", ""), reverse=True)
    if latest and latest[0] > (d.get("bank_balance_at") or "")[:16]:
        d["bank_balance"], d["bank_balance_at"] = latest[1], latest[0]
    if added and d.get("bank_balance") is not None:
        # older rows must not change today's KBank balance (the bank's figure is the truth)
        d["wallets"]["kbank"]["opening"] = round(d["bank_balance"] - sum(signed(it, "kbank") for it in d["items"]), 2)
    return added, skipped
