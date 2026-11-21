"""Small password-protected web page: see status, watch/download/delete recordings, edit settings."""
import hashlib
import hmac
import html
import json
import logging
import mimetypes
import os
import shutil
import time
import urllib.parse
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import notify, record, settings, subs

log = logging.getLogger("web")
E = html.escape
FAILS = {}  # ip -> (count, first_time)
SESSION_DAYS = 30


def _sig(cfg, expiry):
    key = (cfg.get("secret", "") + cfg.get("web_password", "")).encode()
    return hmac.new(key, f"ok|{expiry}".encode(), hashlib.sha256).hexdigest()


def make_token(cfg):
    exp = int(time.time()) + SESSION_DAYS * 86400
    return f"{exp}.{_sig(cfg, exp)}"


def check_token(cfg, token):
    try:
        exp, sig = token.split(".", 1)
        return int(exp) > time.time() and hmac.compare_digest(sig, _sig(cfg, int(exp)))
    except Exception:
        return False


CSS = """
*{box-sizing:border-box}body{font-family:-apple-system,"Segoe UI",Sarabun,"Noto Sans Thai",sans-serif;
margin:0;background:#f5f5f7;color:#1d1d1f;line-height:1.5}
header{background:#111;color:#fff;padding:12px 16px;display:flex;gap:16px;align-items:center;flex-wrap:wrap}
header a{color:#fff;text-decoration:none;opacity:.85}header a:hover{opacity:1}header b{margin-right:auto}
main{max-width:860px;margin:0 auto;padding:16px}
.card{background:#fff;border-radius:12px;padding:14px 16px;margin:0 0 12px;box-shadow:0 1px 2px rgba(0,0,0,.06)}
h2{font-size:17px;margin:20px 0 8px}.muted{color:#6e6e73;font-size:13px}
.pill{display:inline-block;padding:2px 10px;border-radius:99px;font-size:12px;font-weight:600}
.live{background:#ff3b30;color:#fff}.off{background:#e5e5ea;color:#3a3a3c}.err{background:#ffcc00;color:#3a2a00}
.row{display:flex;justify-content:space-between;align-items:center;gap:8px;padding:6px 0;border-bottom:1px solid #f0f0f0}
.row:last-child{border:0}video{width:100%;max-height:70vh;background:#000;border-radius:8px;margin-top:8px}
button,.btn{background:#0071e3;color:#fff;border:0;border-radius:8px;padding:8px 14px;font-size:14px;
cursor:pointer;text-decoration:none;display:inline-block}
.btn.gray{background:#e5e5ea;color:#1d1d1f}.btn.red,button.red{background:#ff3b30}
input,textarea{width:100%;padding:9px;border:1px solid #d2d2d7;border-radius:8px;font-size:15px;font-family:inherit}
label{display:block;font-weight:600;margin:12px 0 4px}pre{white-space:pre-wrap;font-size:12px;overflow:auto}
.actions{display:flex;gap:8px;flex-wrap:wrap;margin-top:8px}code{background:#f0f0f3;padding:1px 6px;border-radius:5px}
select{width:100%;padding:9px;border:1px solid #d2d2d7;border-radius:8px;font-size:15px;background:#fff}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:12px}
.grid .card{margin:0;display:block;color:inherit;text-decoration:none}a.card:hover{box-shadow:0 2px 8px rgba(0,0,0,.12)}
.big{font-size:26px;font-weight:700;margin:4px 0}.two{display:grid;grid-template-columns:1fr 1fr;gap:0 12px}
.mock{background:#fff4d6;color:#7a5600}.warn{background:#ffe5e3;color:#a1150b}.soon{color:#ff3b30;font-weight:600}
.dim{opacity:.5}
"""


def page(title, body, nav=True):
    navhtml = ('<header><b>🪷 ผู้ช่วยส่วนตัว</b><a href="/">หน้าแรก</a>'
               '<a href="/recorder">อัดไลฟ์</a><a href="/subs">Subscriptions</a>'
               '<a href="/settings">ตั้งค่า</a><a href="/log">บันทึกระบบ</a>'
               '<a href="/logout">ออก</a></header>') if nav else \
        '<header><b>🪷 ผู้ช่วยส่วนตัว</b></header>'
    return (f'<!doctype html><html lang="th"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>{E(title)}</title><style>{CSS}</style></head><body>{navhtml}'
            f'<main>{body}</main></body></html>')


def list_recordings():
    out = []
    root = settings.RECORDINGS_DIR
    if not root.exists():
        return out
    for info_file in root.glob("*/*/info.json"):
        info = record.read_info(info_file.parent)
        if info:
            info["_path"] = f"{info_file.parent.parent.name}/{info_file.parent.name}"
            out.append(info)
    out.sort(key=lambda i: i.get("started") or "", reverse=True)
    return out


def thai_time(iso):
    try:
        d = datetime.fromisoformat(iso)
        return d.strftime("%d/%m/%Y %H:%M")
    except Exception:
        return "-"


class Handler(BaseHTTPRequestHandler):
    server_version = "tkrec"
    mgr = None

    def log_message(self, fmt, *args):
        pass

    # ---------- helpers ----------
    def client_ip(self):
        return (self.headers.get("X-Forwarded-For") or self.client_address[0]).split(",")[0].strip()

    def authed(self):
        cfg = settings.load()
        if not cfg.get("web_password"):
            return True
        cookies = dict(c.strip().split("=", 1) for c in (self.headers.get("Cookie") or "").split(";") if "=" in c)
        return check_token(cfg, cookies.get("tkrec", ""))

    def send_html(self, body, code=200, headers=None):
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def redirect(self, to, headers=None):
        self.send_response(303)
        self.send_header("Location", to)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def form(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(min(n, 1_000_000)).decode("utf-8", "replace")
        return {k: v[-1] for k, v in urllib.parse.parse_qs(raw, keep_blank_values=True).items()}

    # ---------- routing ----------
    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/login":
            return self.login_page()
        if path == "/health":
            return self.send_html("ok")
        if not self.authed():
            return self.redirect("/login")
        if path == "/":
            return self.hub()
        if path == "/recorder":
            return self.home()
        if path == "/subs":
            return self.subs_page()
        if path == "/settings":
            return self.settings_page()
        if path == "/log":
            return self.log_page()
        if path == "/logout":
            return self.redirect("/login", {"Set-Cookie": "tkrec=; Max-Age=0; Path=/"})
        if path.startswith("/files/"):
            return self.serve_file(urllib.parse.unquote(path[len("/files/"):]))
        self.send_html(page("ไม่พบ", "<p>ไม่พบหน้านี้</p>"), 404)

    def do_HEAD(self):
        self.do_GET()

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/login":
            return self.login_post()
        if not self.authed():
            return self.redirect("/login")
        f = self.form()
        if path == "/settings":
            return self.settings_post(f)
        if path == "/delete":
            return self.delete_post(f)
        if path == "/test-notify":
            ok = notify.send("🔔 ทดสอบแจ้งเตือน", "ถ้าเห็นข้อความนี้ แปลว่าแจ้งเตือนใช้ได้แล้ว 🎉",
                             tags=["bell"], click=settings.load().get("public_url") or None)
            return self.redirect("/settings?msg=" + ("sent" if ok else "fail"))
        if path == "/check-now":
            self.mgr.last_check.clear()
            return self.redirect("/recorder?msg=checking")
        if path == "/subs/save":
            return self.subs_save(f)
        if path == "/subs/delete":
            return self.subs_delete(f)
        if path == "/subs/options":
            return self.subs_options(f)
        self.send_html("not found", 404)

    # ---------- login ----------
    def login_page(self, error=""):
        err = f'<p style="color:#ff3b30">{E(error)}</p>' if error else ""
        body = (f'<div class="card" style="max-width:380px;margin:40px auto"><h2>เข้าสู่ระบบ</h2>{err}'
                f'<form method="post" action="/login"><label>รหัสผ่าน</label>'
                f'<input type="password" name="password" autofocus autocomplete="current-password">'
                f'<div class="actions"><button>เข้าสู่ระบบ</button></div></form></div>')
        self.send_html(page("เข้าสู่ระบบ", body, nav=False))

    def login_post(self):
        ip = self.client_ip()
        count, first = FAILS.get(ip, (0, time.time()))
        if time.time() - first > 900:
            count, first = 0, time.time()
        if count >= 10:
            return self.login_page("ใส่ผิดหลายครั้งเกินไป รอ 15 นาทีแล้วลองใหม่")
        cfg = settings.load()
        pw = self.form().get("password", "")
        if cfg.get("web_password") and hmac.compare_digest(pw.encode(), cfg["web_password"].encode()):
            FAILS.pop(ip, None)
            secure = "; Secure" if self.headers.get("X-Forwarded-Proto") == "https" else ""
            cookie = (f"tkrec={make_token(cfg)}; Max-Age={SESSION_DAYS * 86400}; Path=/; HttpOnly; "
                      f"SameSite=Lax{secure}")
            return self.redirect("/", {"Set-Cookie": cookie})
        FAILS[ip] = (count + 1, first)
        time.sleep(1)
        self.login_page("รหัสผ่านไม่ถูกต้อง")

    # ---------- pages ----------
    def home(self):
        cfg = settings.load()
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        parts = []
        if q.get("msg") == ["checking"]:
            parts.append('<div class="card">กำลังเช็กทุกบัญชีใหม่… รีเฟรชในอีกสักครู่</div>')
        if q.get("msg") == ["deleted"]:
            parts.append('<div class="card">ลบแล้ว</div>')

        # accounts
        rows = []
        targets = [(u, u, "") for u in cfg["accounts"]] + \
                  [(u, "ig:" + u, ' <span class="pill off">IG</span>') for u in cfg["ig_accounts"]]
        for user, key, tag in targets:
            st = self.mgr.status.get(key, {}) if self.mgr else {}
            state = st.get("state")
            if state == "recording":
                pill = '<span class="pill live">● กำลังอัด</span>'
            elif state == "offline":
                pill = '<span class="pill off">ไม่ได้ไลฟ์</span>'
            elif state == "error":
                pill = f'<span class="pill err" title="{E(st.get("error", ""))}">เช็กไม่ได้</span>'
            else:
                pill = '<span class="pill off">รอเช็ก</span>'
            checked = datetime.fromtimestamp(st["checked"], record.TZ).strftime("%H:%M:%S") if st.get("checked") else "-"
            err = f'<div class="muted">{E(st.get("error", ""))}</div>' if state == "error" else ""
            rows.append(f'<div class="row"><div><b>@{E(user)}</b>{tag} <span class="muted">เช็กล่าสุด {checked}</span>'
                        f'{err}</div>{pill}</div>')
        if not rows:
            rows.append('<p>ยังไม่มีบัญชีที่เฝ้า — <a href="/settings">เพิ่มในหน้าตั้งค่า</a></p>')
        du = shutil.disk_usage(settings.DATA_DIR)
        parts.append(f'<h2>บัญชีที่เฝ้า (TikTok ทุก {cfg["check_interval"]} วิ · IG ทุก {cfg["ig_check_interval"]} วิ)</h2><div class="card">'
                     f'{"".join(rows)}<form method="post" action="/check-now" class="actions">'
                     f'<button class="btn gray">เช็กเดี๋ยวนี้</button></form></div>'
                     f'<p class="muted">พื้นที่ว่าง {du.free / (1 << 30):.1f} GB จาก {du.total / (1 << 30):.0f} GB</p>')

        # recordings
        recs = list_recordings()
        parts.append(f"<h2>ไลฟ์ที่อัดไว้ ({len(recs)})</h2>")
        if not recs:
            parts.append('<div class="card muted">ยังไม่มี — พอมีคนที่เฝ้าไลฟ์ ระบบจะอัดให้อัตโนมัติ</div>')
        for r in recs:
            parts.append(self.rec_card(r))
        self.send_html(page("อัดไลฟ์", "".join(parts)))

    def rec_card(self, r):
        status = r.get("status")
        ig = ' <span class="pill off">IG</span>' if r.get("platform") == "instagram" else ""
        head = (f'<b>@{E(r.get("user", ""))}</b>{ig} {E(r.get("nickname") or "")}'
                f'<div class="muted">{thai_time(r.get("started"))} – '
                f'{thai_time(r["ended"]) if r.get("ended") else "…"} · {E(r.get("title") or "")}</div>')
        if status == "recording":
            return (f'<div class="card">{head}<p><span class="pill live">● กำลังอัด</span> '
                    f'<span class="muted">{r.get("segments", 0)} ท่อน — ไฟล์จะพร้อมหลังไลฟ์จบ</span></p></div>')
        if status == "merging":
            return f'<div class="card">{head}<p class="muted">กำลังรวมไฟล์…</p></div>'
        body = []
        for f in r.get("files", []):
            url = "/files/" + urllib.parse.quote(f'{r["_path"]}/{f["name"]}')
            info = (f'{record.fmt_duration(f.get("duration", 0))} · {record.fmt_size(f["size"])}'
                    f'{" · %sx%s" % (f["width"], f["height"]) if f.get("width") else ""}')
            player = (f'<details><summary>▶️ ดูเลย</summary><video controls preload="none" playsinline '
                      f'src="{url}"></video></details>') if f["name"].endswith(".mp4") else ""
            body.append(f'<div class="row"><div>{E(f["name"])}<div class="muted">{info}</div></div>'
                        f'<a class="btn" href="{url}?dl=1">โหลด</a></div>{player}')
        if status == "failed" and not body:
            body.append('<p class="muted">อัดไม่ได้ — ดูสาเหตุในหน้า "บันทึกระบบ"</p>')
        delete = (f'<form method="post" action="/delete" onsubmit="return confirm(\'ลบไลฟ์นี้ถาวร?\')">'
                  f'<input type="hidden" name="path" value="{E(r["_path"])}">'
                  f'<button class="red" style="padding:4px 10px;font-size:12px">ลบ</button></form>')
        return f'<div class="card"><div class="row"><div>{head}</div>{delete}</div>{"".join(body)}</div>'

    # ---------- hub ----------
    def hub(self):
        cfg = settings.load()
        st = self.mgr.status if self.mgr else {}
        live = [k.replace("ig:", "IG @") if k.startswith("ig:") else "@" + k
                for k, v in st.items() if v.get("state") == "recording"]
        errs = sum(1 for v in st.values() if v.get("state") == "error")
        du = shutil.disk_usage(settings.DATA_DIR)
        free_pct = du.free / du.total * 100 if du.total else 0
        n_acc = len(cfg["accounts"]) + len(cfg["ig_accounts"])
        if live:
            rec_head = f'<span class="pill live">● กำลังอัด {len(live)}</span>'
            rec_body = f'<div>{E(", ".join(live))}</div>'
        else:
            rec_head = '<span class="pill off">ไม่มีใครไลฟ์</span>'
            rec_body = ""
        err_line = f'<div class="muted" style="color:#a1150b">⚠️ เช็กไม่ได้ {errs} บัญชี</div>' if errs else ""
        rec_card = (f'<a class="card" href="/recorder"><div class="row" style="border:0">'
                    f'<b>📼 อัดไลฟ์</b>{rec_head}</div>{rec_body}'
                    f'<div class="muted">เฝ้าอยู่ {n_acc} บัญชี · ไลฟ์ที่เก็บไว้ {len(list_recordings())}</div>'
                    f'<div class="muted">พื้นที่ว่าง {du.free / (1 << 30):.1f} GB ({free_pct:.0f}%)</div>'
                    f'{err_line}</a>')

        data = subs.load()
        month, year = subs.totals(data)
        up = subs.upcoming(data, 30)
        lines = []
        for n, it in up[:4]:
            when = "วันนี้" if n == 0 else f"อีก {n} วัน"
            cls = ' class="soon"' if n <= int(data.get("remind_days") or 3) else ""
            warn = " ⚠️" if it.get("status") == "cancelling" else ""
            lines.append(f'<div class="row"><span>{E(it["name"])}{warn}</span><span{cls}>{when}</span></div>')
        up_html = "".join(lines) or '<div class="muted">30 วันข้างหน้าไม่มีตัดบัตร</div>'
        n_cancel = sum(1 for it in data["items"] if it.get("status") == "cancelling")
        cancel_line = f'<div class="muted">⚠️ รอยกเลิก {n_cancel} รายการ</div>' if n_cancel else ""
        subs_card = (f'<a class="card" href="/subs"><b>💳 Subscriptions</b>'
                     f'<div class="big">{subs.fmt_money(month)}<span class="muted"> / เดือน</span></div>'
                     f'<div class="muted">≈ {subs.fmt_money(year)} / ปี</div>{cancel_line}'
                     f'<h2 style="font-size:14px;margin:12px 0 4px">ตัดบัตรเร็ว ๆ นี้</h2>{up_html}</a>')

        likes_card = ('<div class="card dim"><b>▶️ YouTube Likes → X</b>'
                      '<p class="muted">เร็ว ๆ นี้ — กดไลค์คลิปแล้วเด้งแจ้งเตือนให้กดโพสต์ X</p></div>')
        now = datetime.now(record.TZ).strftime("%d/%m/%Y %H:%M")
        body = (f'<p class="muted">อัปเดต {now}</p>'
                f'<div class="grid">{rec_card}{subs_card}{likes_card}</div>')
        self.send_html(page("ผู้ช่วยส่วนตัว", body))

    # ---------- subscriptions ----------
    def subs_page(self):
        data = subs.load()
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        msg = {"saved": "บันทึกแล้ว ✅", "deleted": "ลบแล้ว"}.get((q.get("msg") or [""])[0], "")
        edit_id = (q.get("edit") or [""])[0]
        rate = float(data.get("usd_rate") or 33)
        month, year = subs.totals(data)
        parts = [f'<div class="card">{E(msg)}</div>'] if msg else []
        parts.append(f'<div class="card two"><div><div class="muted">ต่อเดือน</div>'
                     f'<div class="big">{subs.fmt_money(month)}</div></div><div><div class="muted">ต่อปี</div>'
                     f'<div class="big">{subs.fmt_money(year)}</div></div>'
                     f'<div class="muted" style="grid-column:1/3">นับเฉพาะรายเดือน/รายปีที่ยังไม่ยกเลิก · '
                     f'1 USD = {rate:g} บาท</div></div>')
        if any(it.get("mock") for it in data["items"]):
            parts.append('<div class="card mock">รายการที่มีป้าย <b>ตัวอย่าง</b> ราคา/วันที่ยังไม่ใช่ของจริง '
                         '— กด "แก้" แล้วใส่ข้อมูลจริง ป้ายจะหายไปเอง</div>')

        order = {"cancelling": 0, "active": 1, "cancelled": 2}
        items = sorted(data["items"], key=lambda it: (order.get(it.get("status"), 1),
                                                      subs.days_left(it) if subs.days_left(it) is not None else 9999,
                                                      it.get("name", "")))
        for status, label in subs.STATUSES.items():
            group = [it for it in items if it.get("status") == status]
            if not group:
                continue
            parts.append(f'<h2>{label} ({len(group)})</h2>')
            for it in group:
                parts.append(self.sub_row(it, rate, data))

        editing = next((it for it in data["items"] if it["id"] == edit_id), None)
        parts.append(f'<h2 id="form">{"แก้ไข: " + E(editing["name"]) if editing else "เพิ่มรายการ"}</h2>')
        parts.append(self.sub_form(editing))
        parts.append(f'''<h2>ตั้งค่า</h2><form method="post" action="/subs/options" class="card two">
<div><label>เรต USD → บาท</label><input name="usd_rate" type="number" step="0.01" min="1" value="{rate:g}"></div>
<div><label>เตือนก่อนตัดบัตรกี่วัน</label><input name="remind_days" type="number" min="0" max="30"
 value="{int(data.get("remind_days") or 3)}"></div>
<div class="actions"><button>บันทึก</button></div></form>
<p class="muted">แจ้งเตือนส่งผ่านแอป ntfy ตัวเดียวกับระบบอัดไลฟ์ · พอเลยวันต่ออายุ ระบบจะเลื่อนวันไปรอบถัดไปให้เอง
(เฉพาะสถานะ "ใช้อยู่")</p>''')
        self.send_html(page("Subscriptions", "".join(parts)))

    def sub_row(self, it, rate, data):
        thb = subs.to_thb(it, rate)
        cycle = subs.CYCLES.get(it.get("cycle"), "")
        if it.get("cycle") == "free":
            price = "ฟรี"
        elif not it.get("price"):
            price = f"฿0 · {cycle}"
        else:
            raw = (f'{float(it["price"]):,.2f} USD' if it.get("currency") == "USD"
                   else subs.fmt_money(float(it["price"])))
            conv = f' ≈ {subs.fmt_money(thb)}' if it.get("currency") == "USD" else ""
            price = f"{raw}{conv} · {cycle}"
        n = subs.days_left(it)
        if n is None:
            when = ""
        elif n < 0:
            when = f' · เลยวันต่ออายุมา {-n} วัน'
        else:
            soon = n <= int(data.get("remind_days") or 3)
            txt = "ต่ออายุวันนี้" if n == 0 else f"ต่ออายุอีก {n} วัน"
            when = f' · <span class="{"soon" if soon else ""}">{txt}</span> ({E(it["next"])})'
        tags = ""
        if it.get("mock"):
            tags += ' <span class="pill mock">ตัวอย่าง</span>'
        if it.get("category"):
            tags += f' <span class="pill off">{E(it["category"])}</span>'
        note = f'<div class="muted">{E(it["note"])}</div>' if it.get("note") else ""
        dim = " dim" if it.get("status") == "cancelled" else ""
        return (f'<div class="card{dim}"><div class="row" style="border:0;align-items:flex-start"><div>'
                f'<b>{E(it["name"])}</b>{tags}<div class="muted">{price}{when}</div>{note}</div>'
                f'<a class="btn gray" style="padding:4px 12px;font-size:13px" '
                f'href="/subs?edit={E(it["id"])}#form">แก้</a></div></div>')

    def sub_form(self, it):
        it = it or {"id": "", "name": "", "price": "", "currency": "THB", "cycle": "monthly",
                    "next": "", "category": "", "status": "active", "note": ""}

        def opts(choices, current):
            return "".join(f'<option value="{k}"{" selected" if k == current else ""}>{E(v)}</option>'
                           for k, v in choices.items())
        cur = {c: c for c in subs.CURRENCIES}
        price = "" if it.get("price") in ("", None) else f'{float(it["price"]):g}'
        delete = ""
        if it["id"]:
            delete = (f'<form method="post" action="/subs/delete" onsubmit="return confirm(\'ลบรายการนี้?\')">'
                      f'<input type="hidden" name="id" value="{E(it["id"])}">'
                      f'<button class="red">ลบรายการนี้</button></form>')
        cancel = '<a class="btn gray" href="/subs">ยกเลิกการแก้</a>' if it["id"] else ""
        return f'''<div class="card"><form method="post" action="/subs/save">
<input type="hidden" name="id" value="{E(it["id"])}">
<label>ชื่อบริการ</label><input name="name" required value="{E(it["name"])}" placeholder="เช่น Netflix">
<div class="two"><div><label>ราคา</label><input name="price" type="number" step="0.01" min="0" value="{price}"></div>
<div><label>สกุลเงิน</label><select name="currency">{opts(cur, it.get("currency"))}</select></div>
<div><label>รอบจ่าย</label><select name="cycle">{opts(subs.CYCLES, it.get("cycle"))}</select></div>
<div><label>วันต่ออายุถัดไป</label><input name="next" type="date" value="{E(it.get("next", ""))}"></div>
<div><label>หมวด</label><input name="category" value="{E(it.get("category", ""))}" placeholder="เช่น AI, ดูหนัง"></div>
<div><label>สถานะ</label><select name="status">{opts(subs.STATUSES, it.get("status"))}</select></div></div>
<label>โน้ต</label><input name="note" value="{E(it.get("note", ""))}">
<div class="actions"><button>บันทึก</button>{cancel}</div></form>{delete}</div>'''

    def subs_save(self, f):
        name = f.get("name", "").strip()
        if not name:
            return self.redirect("/subs")
        try:
            price = max(0.0, float(f.get("price") or 0))
        except ValueError:
            price = 0.0
        nxt = f.get("next", "").strip()
        row = {"name": name[:80], "price": price,
               "currency": f.get("currency") if f.get("currency") in subs.CURRENCIES else "THB",
               "cycle": f.get("cycle") if f.get("cycle") in subs.CYCLES else "monthly",
               "next": nxt if subs.parse_date(nxt) else "",
               "category": f.get("category", "").strip()[:40],
               "status": f.get("status") if f.get("status") in subs.STATUSES else "active",
               "note": f.get("note", "").strip()[:300], "mock": False}
        data = subs.load()
        item = next((it for it in data["items"] if it["id"] == f.get("id")), None)
        if item:
            item.update(row)
        else:
            row["id"] = os.urandom(4).hex()
            data["items"].append(row)
        subs.save(data)
        self.redirect("/subs?msg=saved")

    def subs_delete(self, f):
        data = subs.load()
        data["items"] = [it for it in data["items"] if it["id"] != f.get("id")]
        subs.save(data)
        self.redirect("/subs?msg=deleted")

    def subs_options(self, f):
        data = subs.load()
        try:
            data["usd_rate"] = max(1.0, float(f.get("usd_rate") or 33))
            data["remind_days"] = min(30, max(0, int(f.get("remind_days") or 3)))
        except ValueError:
            pass
        subs.save(data)
        self.redirect("/subs?msg=saved")

    def settings_page(self):
        cfg = settings.load()
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        msg = {"saved": "บันทึกแล้ว ✅", "sent": "ส่งแจ้งเตือนทดสอบแล้ว — ดูในแอป ntfy",
               "fail": "ส่งแจ้งเตือนไม่สำเร็จ", "pw": "รหัสผ่านใหม่ต้องยาวอย่างน้อย 8 ตัว"}.get(
            (q.get("msg") or [""])[0], "")
        msghtml = f'<div class="card">{E(msg)}</div>' if msg else ""
        topic = cfg.get("ntfy_topic", "")
        body = f"""{msghtml}
<form method="post" action="/settings" class="card">
<label>บัญชี TikTok ที่จะเฝ้า (บรรทัดละ 1 ชื่อ จะมี @ หรือวางลิงก์โปรไฟล์ก็ได้)</label>
<textarea name="accounts" rows="6" placeholder="เช่น&#10;@username1&#10;username2">{E(chr(10).join(cfg["accounts"]))}</textarea>
<label>เช็กทุกกี่วินาที (ต่ำสุด 20 · แนะนำ 60)</label>
<input name="check_interval" type="number" min="20" value="{cfg["check_interval"]}">
<label>ลบไลฟ์ที่เก่ากว่ากี่วัน (0 = ไม่ลบเลย)</label>
<input name="retention_days" type="number" min="0" value="{cfg.get("retention_days", 0)}">
<label>ถ้าดิสก์ว่างน้อยกว่ากี่ GB ให้ลบไลฟ์เก่าสุดอัตโนมัติ</label>
<input name="min_free_gb" type="number" min="0" value="{cfg.get("min_free_gb", 10)}">
<label>บัญชี Instagram ที่จะเฝ้า (บรรทัดละ 1 ชื่อ)</label>
<textarea name="ig_accounts" rows="4" placeholder="เช่น&#10;@username1">{E(chr(10).join(cfg["ig_accounts"]))}</textarea>
<label>คุกกี้ Instagram (จำเป็นสำหรับ IG — ใช้บัญชีสำรอง)</label>
<textarea name="ig_cookies" rows="3" placeholder="sessionid=...; ds_user_id=...; csrftoken=...">{E(cfg.get("ig_cookies", ""))}</textarea>
<label>เช็ก IG ทุกกี่วินาที (ต่ำสุด 45 · แนะนำ 90)</label>
<input name="ig_check_interval" type="number" min="45" value="{cfg["ig_check_interval"]}">
<label>คุกกี้ TikTok (ไม่บังคับ — ใส่เมื่อเช็กไม่ได้หรือเจอไลฟ์ 18+)</label>
<textarea name="cookies" rows="3" placeholder="sessionid=...; tt-target-idc=...">{E(cfg.get("cookies", ""))}</textarea>
<label>เปลี่ยนรหัสผ่านหน้าเว็บ (เว้นว่าง = ใช้รหัสเดิม)</label>
<input name="new_password" type="password" autocomplete="new-password">
<div class="actions"><button>บันทึก</button></div></form>
<div class="card"><h2 style="margin-top:0">แจ้งเตือน (แอป ntfy)</h2>
<p>ในแอป ntfy กด <b>+</b> แล้วใส่ชื่อ topic นี้:</p><p><code style="font-size:16px">{E(topic)}</code></p>
<p class="muted">ใครรู้ชื่อนี้ก็เห็นแจ้งเตือนได้ อย่าแชร์ต่อ</p>
<form method="post" action="/test-notify" class="actions"><button class="btn gray">ส่งแจ้งเตือนทดสอบ</button></form></div>
"""
        self.send_html(page("ตั้งค่า", body))

    def settings_post(self, f):
        cfg = settings.load()
        accounts = []
        for line in f.get("accounts", "").replace(",", "\n").splitlines():
            u = settings.clean_username(line)
            if u and u not in accounts:
                accounts.append(u)
        ig_accounts = []
        for line in f.get("ig_accounts", "").replace(",", "\n").splitlines():
            u = settings.clean_username(line)
            if u and u not in ig_accounts:
                ig_accounts.append(u)
        changes = {
            "accounts": accounts,
            "ig_accounts": ig_accounts,
            "ig_cookies": f.get("ig_cookies", "").strip(),
            "ig_check_interval": max(45, int(f.get("ig_check_interval") or 90)),
            "check_interval": max(20, int(f.get("check_interval") or 60)),
            "retention_days": max(0, int(f.get("retention_days") or 0)),
            "min_free_gb": max(0, float(f.get("min_free_gb") or 0)),
            "cookies": f.get("cookies", "").strip(),
        }
        headers = None
        new_pw = f.get("new_password", "")
        if new_pw:
            if len(new_pw) < 8:
                return self.redirect("/settings?msg=pw")
            changes["web_password"] = new_pw
        cfg.update(changes)
        settings.save(cfg)
        if new_pw:  # old sessions become invalid; give this browser a fresh one
            headers = {"Set-Cookie": f"tkrec={make_token(cfg)}; Max-Age={SESSION_DAYS * 86400}; Path=/; "
                                     f"HttpOnly; SameSite=Lax"}
        self.redirect("/settings?msg=saved", headers)

    def delete_post(self, f):
        rel = f.get("path", "")
        target = (settings.RECORDINGS_DIR / rel).resolve()
        root = settings.RECORDINGS_DIR.resolve()
        if root in target.parents and target.parent.parent == root and target.is_dir():
            info = record.read_info(target) or {}
            if info.get("status") != "recording":
                shutil.rmtree(target, ignore_errors=True)
                log.info("deleted from web: %s", rel)
        self.redirect("/recorder?msg=deleted")

    def log_page(self):
        try:
            lines = settings.LOG_FILE.read_text("utf-8", "replace").splitlines()[-300:]
        except Exception:
            lines = ["(ยังไม่มีบันทึก)"]
        body = f'<div class="card"><pre>{E(chr(10).join(reversed(lines)))}</pre></div>'
        self.send_html(page("บันทึกระบบ", '<h2>บันทึกระบบ (ล่าสุดอยู่บน)</h2>' + body))

    # ---------- files with Range support (needed for video seeking on phones) ----------
    def serve_file(self, rel):
        root = settings.RECORDINGS_DIR.resolve()
        path = (root / rel).resolve()
        if root not in path.parents or not path.is_file():
            return self.send_html("not found", 404)
        size = path.stat().st_size
        ctype = mimetypes.guess_type(path.name)[0] or ("video/mp2t" if path.suffix == ".ts" else
                                                      "application/octet-stream")
        start, end = 0, size - 1
        rng = self.headers.get("Range")
        code = 200
        if rng and rng.startswith("bytes="):
            try:
                a, b = rng[6:].split(",")[0].split("-")
                if a:
                    start = int(a)
                    end = int(b) if b else size - 1
                else:
                    start = max(0, size - int(b))
                end = min(end, size - 1)
                if start > end:
                    raise ValueError
                code = 206
            except ValueError:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.end_headers()
                return
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        if code == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        if "dl=1" in (urllib.parse.urlparse(self.path).query or ""):
            self.send_header("Content-Disposition",
                             f"attachment; filename*=UTF-8''{urllib.parse.quote(path.name)}")
        self.end_headers()
        if self.command == "HEAD":
            return
        with open(path, "rb") as fh:
            fh.seek(start)
            left = end - start + 1
            try:
                while left > 0:
                    chunk = fh.read(min(1 << 20, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass


def serve(mgr, host="127.0.0.1", port=8080):
    Handler.mgr = mgr
    srv = ThreadingHTTPServer((host, port), Handler)
    srv.daemon_threads = True
    log.info("web on http://%s:%s", host, port)
    srv.serve_forever()
