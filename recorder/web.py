"""Small password-protected web page: see status, watch/download/delete recordings, edit settings."""
import hashlib
import hmac
import html
import json
import logging
import mimetypes
import os
import shutil
import threading
import time
import urllib.parse
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import likes, notify, record, settings, subs

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


FONTS = ('<link rel="preconnect" href="https://fonts.googleapis.com">'
         '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
         '<link href="https://fonts.googleapis.com/css2?family=Geist:wght@300;400;500;600'
         '&family=Geist+Mono:wght@400;500&family=Anuphan:wght@300;400;500;600&display=swap" rel="stylesheet">')

# Soft "bento" app look: warm grey canvas, big rounded white cards, near-black dark surfaces,
# one pastel colour per section (recorder coral, subscriptions lime, likes lavender), light big numbers.
CSS = """
:root{--canvas:#ebeae5;--card:#fff;--soft:#f4f3ef;--soft2:#e9e8e2;--ink:#151515;--ink2:#3a3a38;--muted:#6f6e69;
--dark:#1c1c1c;--lime:#d3e27f;--line:#e2e1db;
--rec:#ffb4a3;--rec-ink:#b2321d;--subs:#d3e27f;--subs-ink:#4f5d0f;--likes:#d7cdfb;--likes-ink:#5130b8;
--acc:var(--lime);--acc-ink:var(--ink);
--sans:"Geist","Anuphan",system-ui,-apple-system,"Segoe UI",sans-serif;
--mono:"Geist Mono","Anuphan",ui-monospace,Menlo,monospace;color-scheme:light}
.k-rec{--acc:var(--rec);--acc-ink:var(--rec-ink)}.k-subs{--acc:var(--subs);--acc-ink:var(--subs-ink)}
.k-likes{--acc:var(--likes);--acc-ink:var(--likes-ink)}
*{box-sizing:border-box}html,body{margin:0;overflow-x:clip}
body{background:var(--canvas);color:var(--ink);font-family:var(--sans);font-size:15px;line-height:1.5;-webkit-font-smoothing:antialiased}
a{color:inherit;text-decoration:none}:focus-visible{outline:2px solid var(--ink);outline-offset:3px;border-radius:8px}
.wrap{max-width:1040px;margin:0 auto;padding:0 clamp(14px,3vw,32px)}

/* top bar */
.top{display:flex;align-items:center;justify-content:space-between;gap:12px;padding-top:18px;padding-bottom:6px}
.brand{display:flex;align-items:center;gap:10px;font-weight:600;letter-spacing:-.02em}
.brand i{width:38px;height:38px;border-radius:50%;background:conic-gradient(#ffb4a3,#d3e27f,#d7cdfb,#ffb4a3);display:block}
.brand small{display:block;font-size:12px;font-weight:400;color:var(--muted);letter-spacing:0}
.seg{display:flex;gap:4px;background:var(--card);padding:5px;border-radius:999px}
.seg a{display:flex;align-items:center;gap:7px;padding:8px 14px;border-radius:999px;font-size:14px;font-weight:500;color:var(--ink2);white-space:nowrap;transition:background .2s}
.seg a:hover{background:var(--soft)}.seg a.on{background:var(--dark);color:#fff}
.seg svg{width:17px;height:17px}
.ibtn{width:42px;height:42px;border-radius:50%;background:var(--card);display:grid;place-items:center;flex:none}
.ibtn svg{width:19px;height:19px}.ibtn:hover{background:var(--soft2)}
.tools{display:flex;gap:8px}
/* mobile: floating dark pill nav at the bottom */
.dock{display:none}
@media(max-width:760px){
 .seg{display:none}
 .dock{display:flex;position:fixed;left:50%;bottom:14px;transform:translateX(-50%);background:var(--dark);border-radius:999px;padding:6px;gap:4px;z-index:9;box-shadow:0 10px 30px rgba(0,0,0,.25)}
 .dock a{width:52px;height:52px;border-radius:50%;display:grid;place-items:center;color:#cfcfcf}
 .dock a svg{width:21px;height:21px}.dock a.on{background:#fff;color:var(--ink)}
 main.wrap{padding-bottom:120px!important}
}

/* page title */
.hero{display:flex;justify-content:space-between;align-items:flex-end;gap:8px 20px;flex-wrap:wrap;padding-top:14px;padding-bottom:8px}
.hero h1{margin:0;font-size:26px;font-weight:600;letter-spacing:-.03em;line-height:1.15}
.hero .sub{font-size:13px;color:var(--ink2);background:var(--card);padding:6px 14px;border-radius:999px}
main.wrap{padding-top:8px;padding-bottom:80px}

/* cards */
.card{background:var(--card);border-radius:22px;padding:14px 16px;margin:0 0 10px}
.card.dark{background:var(--dark);color:#fff}.card.dark .muted{color:#a9a9a4}
.card.tint{background:var(--acc);color:var(--acc-ink)}
h2{font-size:14px;font-weight:600;color:var(--ink);margin:18px 4px 8px;letter-spacing:-.01em}
.card h2{margin:0 0 10px}
.title{font-size:15px;font-weight:500;letter-spacing:-.01em}
.muted{color:var(--muted);font-size:13px}.mono{font-family:var(--mono)}
.pill{display:inline-flex;align-items:center;gap:6px;padding:4px 11px;border-radius:999px;font-size:12px;font-weight:500;
background:var(--soft2);color:var(--ink2);vertical-align:middle;white-space:nowrap}
.pill.live{background:#ffe0d8;color:#b2321d}.pill.live::before{content:"";width:7px;height:7px;border-radius:50%;background:#ff4f2e;animation:blink 1.4s infinite}
.pill.err{background:#ffeec2;color:#7a5200}.pill.mock{background:#efe7da;color:#6e5536}.pill.ok{background:#e3f0b7;color:#435207}
.pill.acc{background:var(--acc);color:var(--acc-ink)}
@keyframes blink{50%{opacity:.25}}
/* lists: each row is its own soft rounded block */
.row{display:flex;justify-content:space-between;align-items:center;gap:12px;padding:9px 12px;background:var(--soft);border-radius:14px;margin-top:6px}
.row:first-child{margin-top:0}.card>.row:only-child{margin:0}
.row.plain{background:none;padding:0;border-radius:0;margin:0}
video{width:100%;max-height:70vh;background:#000;border-radius:18px;margin-top:10px}
button,.btn{font:inherit;font-size:14px;font-weight:500;background:var(--dark);color:#fff;border:0;border-radius:999px;padding:10px 20px;
cursor:pointer;display:inline-flex;align-items:center;gap:6px;transition:transform .15s,opacity .2s;white-space:nowrap}
button:hover,.btn:hover{opacity:.88}button:active,.btn:active{transform:scale(.97)}
.btn.gray,button.gray{background:var(--soft2);color:var(--ink)}
.btn.acc,button.acc{background:var(--acc);color:var(--acc-ink)}
.btn.red,button.red{background:#ffe0d8;color:#b2321d}
.btn.sm,button.sm{padding:7px 14px;font-size:13px}
input,textarea,select{width:100%;padding:10px 12px;border:0;border-radius:14px;font:inherit;font-size:15px;background:var(--soft);color:var(--ink);
outline:2px solid transparent;transition:outline-color .2s,background .2s}
input:focus,textarea:focus,select:focus{outline-color:var(--ink);background:#fff}
input[type=checkbox]{width:18px;height:18px;margin:0 8px 0 0;vertical-align:-3px;accent-color:var(--dark)}
label{display:block;font-size:13px;font-weight:500;color:var(--ink2);margin:12px 2px 5px}
label code{font-size:12px}
pre{white-space:pre-wrap;font-family:var(--mono);font-size:12px;overflow:auto;margin:0;line-height:1.6}
.actions{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-top:12px}
code{font-family:var(--mono);background:var(--soft2);padding:2px 8px;border-radius:8px;font-size:.92em}
.two{display:grid;grid-template-columns:1fr 1fr;gap:0 12px}
.soon{color:#c2361d;font-weight:500}.dim{opacity:.5}
.note{background:var(--acc);color:var(--acc-ink)}.note .muted{color:inherit;opacity:.8}
.flash{background:var(--dark);color:#fff;font-size:14px;border-radius:999px;padding:9px 18px;margin:0 0 10px}
ol.steps{margin:0;padding-left:20px}ol.steps li{margin:8px 0}
.code-big{font-family:var(--mono);font-size:32px;letter-spacing:.14em;font-weight:500;margin:8px 0;background:var(--acc);color:var(--acc-ink);display:inline-block;padding:8px 20px;border-radius:18px}
details summary{cursor:pointer;color:var(--ink2);font-size:14px;margin-top:10px}
/* compact lists: rows separated by hairlines instead of separate blocks */
.card.list{padding:2px 16px}
.card.list>.row{background:none;border-radius:0;padding:10px 0;margin:0;border-top:1px solid var(--line)}
.card.list>.row:first-child{border-top:0}
.card.list>a.row{margin:0 -16px;padding:10px 16px}.card.list>a.row:hover{background:var(--soft)}
.card.list>a.row:first-child{border-radius:20px 20px 0 0}.card.list>a.row:last-child{border-radius:0 0 20px 20px}
.card.list>a.row:only-child{border-radius:20px}
.line2{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.row .pill{padding:2px 9px;font-size:11px}
.price{font-family:var(--mono);font-size:14px;font-weight:500;text-align:right;flex:none}
.price small{display:block;font-size:12px;font-weight:400;color:var(--muted)}
.bar .btn,.bar button{background:var(--card);color:var(--ink)}
/* collapsible sections */
details.fold{margin:18px 0 0}details.fold>summary{list-style:none;font-size:14px;font-weight:600;color:var(--ink);margin:0 4px 8px;display:flex;align-items:center;gap:6px}
details.fold>summary::-webkit-details-marker{display:none}
details.fold>summary::before{content:"›";display:inline-block;transition:transform .2s;color:var(--muted);font-size:18px;line-height:1}
details.fold[open]>summary::before{transform:rotate(90deg)}
/* one-line summary strip */
.sumline{display:flex;align-items:baseline;gap:6px 22px;flex-wrap:wrap}
.sumline b{font-size:28px;font-weight:300;letter-spacing:-.03em}.sumline b small{font-size:13px;color:#a9a9a4;font-weight:400;margin-left:4px;letter-spacing:0}
.sumline .muted{margin-left:auto}

/* hub bento */
.bento{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px;margin-top:6px}
.bento .card{margin:0;min-width:0;display:flex;flex-direction:column;gap:2px;transition:transform .2s}
.bento .row{background:none;border-radius:0;padding:7px 0;margin:0;border-top:1px solid var(--line);font-size:14px}
.bento .row .muted,.bento .row>div{min-width:0}.clip{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.bento a.card:hover{transform:translateY(-2px)}
.bento .wide{grid-column:1/-1}
@media(max-width:900px){.bento{grid-template-columns:repeat(2,minmax(0,1fr))}}
@media(max-width:600px){.bento{grid-template-columns:minmax(0,1fr)}}
.chead{display:flex;justify-content:space-between;align-items:center;margin-bottom:4px}
.ico{width:36px;height:36px;border-radius:50%;background:var(--acc);color:var(--acc-ink);display:grid;place-items:center;flex:none}
.ico svg{width:18px;height:18px}
.ctitle{display:flex;align-items:center;gap:10px;font-weight:600;font-size:15px;letter-spacing:-.01em}
.ctitle small{display:block;font-weight:400;font-size:12px;color:var(--muted)}
.big{font-size:38px;font-weight:300;letter-spacing:-.04em;line-height:1;margin:6px 0 8px}
.card.dark .big{font-size:clamp(28px,8vw,38px)}
.big small{font-size:15px;color:var(--muted);letter-spacing:0;font-weight:400;margin-left:4px}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:8px}
.stat{background:rgba(255,255,255,.08);border-radius:14px;padding:8px 12px;min-width:0}
.stat b{display:block;font-size:clamp(32px,9vw,40px);line-height:1.1;font-weight:300;letter-spacing:-.03em}.stat span{font-size:12px;color:#a9a9a4}
.hello{font-size:20px;font-weight:500;letter-spacing:-.02em;line-height:1.2;margin:0}
"""

ICONS = {
    "home": '<path d="M3 10.5 12 3l9 7.5V20a1 1 0 0 1-1 1h-5v-6H9v6H4a1 1 0 0 1-1-1z"/>',
    "rec": '<circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="3.5" fill="currentColor"/>',
    "card": '<rect x="3" y="5" width="18" height="14" rx="3"/><path d="M3 10h18M7 15h4"/>',
    "heart": '<path d="M12 20s-7-4.4-7-10a4 4 0 0 1 7-2.6A4 4 0 0 1 19 10c0 5.6-7 10-7 10z"/>',
    "gear": '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z"/>',
    "log": '<path d="M5 4h14v16H5z"/><path d="M8.5 8.5h7M8.5 12h7M8.5 15.5h4"/>',
    "out": '<path d="M15 4h4v16h-4M10 8l-4 4 4 4M6 12h10"/>',
    "play": '<path d="M8 5.5v13l11-6.5z"/>',
}


def icon(name):
    return (f'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" '
            f'stroke-linecap="round" stroke-linejoin="round">{ICONS[name]}</svg>')


NAV = [("/", "Hub", "home"), ("/recorder", "อัดไลฟ์", "rec"), ("/likes", "Likes", "heart"),
       ("/subs", "Subscriptions", "card")]
SECTION = {"/recorder": "k-rec", "/subs": "k-subs", "/likes": "k-likes"}


def page(title, body, nav=True, active="", heading=None, sub=""):
    seg = dock = tools = ""
    if nav:
        seg = "".join(f'<a href="{h}"{" class=on" if h == active else ""}>{icon(i)}{E(t)}</a>' for h, t, i in NAV)
        seg = f'<nav class="seg">{seg}</nav>'
        dock = "".join(f'<a href="{h}" aria-label="{E(t)}"{" class=on" if h == active else ""}>{icon(i)}</a>'
                       for h, t, i in NAV)
        dock = f'<nav class="dock">{dock}</nav>'
        tools = (f'<div class="tools"><a class="ibtn" href="/settings" title="ตั้งค่าระบบ">{icon("gear")}</a>'
                 f'<a class="ibtn" href="/log" title="บันทึกระบบ">{icon("log")}</a>'
                 f'<a class="ibtn" href="/logout" title="ออกจากระบบ">{icon("out")}</a></div>')
    now = datetime.now(record.TZ)
    hero = (f'<div class="wrap hero"><h1>{E(heading)}</h1>{f"<div class=sub>{sub}</div>" if sub else ""}</div>'
            if heading else "")
    return (f'<!doctype html><html lang="th"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<meta name="theme-color" content="#ebeae5"><title>{E(title)}</title>{FONTS}<style>{CSS}</style></head>'
            f'<body class="{SECTION.get(active, "")}"><header class="wrap top">'
            f'<a class="brand" href="/"><i></i><span>spongelotus<small>{now.strftime("%a %d %b")}</small></span></a>'
            f'{seg}{tools}</header>{hero}<main class="wrap">{body}</main>{dock}</body></html>')


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
        if path == "/about":
            return self.about_page()
        if path == "/privacy":
            return self.privacy_page()
        if path == "/likes/go" and not self.authed():
            return self.likes_go(need_token=True)
        if not self.authed():
            return self.redirect("/login")
        if path == "/":
            return self.hub()
        if path == "/recorder":
            return self.home()
        if path == "/subs":
            return self.subs_page()
        if path == "/likes":
            return self.likes_page()
        if path == "/likes/go":
            return self.likes_go()
        if path == "/recorder/settings":
            return self.rec_settings_page()
        if path == "/settings":
            return self.settings_page()
        if path == "/log":
            return self.log_page()
        if path == "/logout":
            return self.redirect("/login", {"Set-Cookie": "tkrec=; Max-Age=0; Path=/"})
        if path.startswith("/files/"):
            return self.serve_file(urllib.parse.unquote(path[len("/files/"):]))
        self.send_html(page("ไม่พบ", '<p class="muted">ไม่พบหน้านี้</p>', heading="404"), 404)

    def do_HEAD(self):
        self.do_GET()

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/login":
            return self.login_post()
        if not self.authed():
            return self.redirect("/login")
        f = self.form()
        if path == "/recorder/settings":
            return self.rec_settings_post(f)
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
        if path.startswith("/likes/"):
            return self.likes_post(path, f)
        self.send_html("not found", 404)

    # ---------- login ----------
    # ---------- public pages (needed for Google OAuth "In production") ----------
    def about_page(self):
        body = ('<div class="card" style="max-width:680px;margin:0 0 40px">'
                '<p style="margin-top:0"><b>spongelotus Hub</b> เป็นเว็บผู้ช่วยส่วนตัวของเจ้าของคนเดียว '
                'ไม่เปิดให้บุคคลทั่วไปสมัครใช้งาน</p>'
                '<p>ส่วน <b>Likes</b> ใช้ YouTube Data API (สิทธิ์อ่านอย่างเดียว) เพื่อดูรายการคลิปที่เจ้าของกดไลค์ '
                'แล้วส่งแจ้งเตือนไปที่มือถือของเจ้าของ ให้กดโพสต์ลิงก์คลิปลง X ด้วยตัวเอง</p>'
                '<p lang="en" class="muted">spongelotus Hub is a private, single-user personal assistant. '
                'Its Likes feature uses the YouTube Data API (read-only) to see videos the owner has liked '
                'and sends the owner a phone notification to share them on X manually.</p>'
                '<div class="actions"><a class="btn gray" href="/privacy">นโยบายความเป็นส่วนตัว / Privacy policy</a>'
                '<a class="btn" href="/login">เข้าสู่ระบบ</a></div></div>')
        self.send_html(page("เกี่ยวกับ spongelotus Hub", body, nav=False, heading="spongelotus Hub"))

    def privacy_page(self):
        body = ('<div class="card" style="max-width:680px;margin:0 0 40px">'
                '<p class="muted" style="margin-top:0">ปรับปรุงล่าสุด 22 พฤศจิกายน 2026</p>'
                '<p>spongelotus Hub เป็นเว็บที่ใช้ส่วนตัวโดยเจ้าของคนเดียว นโยบายนี้อธิบายว่าแอปใช้ข้อมูลจากบัญชี Google อย่างไร</p>'
                '<h3>ข้อมูลที่เข้าถึง</h3><p>เมื่อเจ้าของเชื่อมบัญชี YouTube แอปขอสิทธิ์ '
                '<code>youtube.readonly</code> (อ่านอย่างเดียว) เพื่ออ่านชื่อช่องของเจ้าของ และรายการคลิปที่กดไลค์ '
                '(ชื่อคลิปและลิงก์) เท่านั้น แอปไม่โพสต์ แก้ไข หรือลบอะไรใน YouTube</p>'
                '<h3>การใช้ข้อมูล</h3><p>ใช้เพื่อส่งแจ้งเตือนเรื่องคลิปที่ไลค์ไปยังมือถือของเจ้าของ '
                'เพื่อให้เจ้าของเลือกกดโพสต์ลิงก์ลง X ด้วยตัวเองเท่านั้น</p>'
                '<h3>การจัดเก็บและการแชร์</h3><p>Token และรายการคลิปที่ส่งแจ้งเตือนแล้วเก็บไว้บนเซิร์ฟเวอร์ส่วนตัวของเจ้าของเท่านั้น '
                'ไม่ขาย ไม่แชร์ ไม่ส่งต่อให้บุคคลอื่น และไม่ใช้เพื่อโฆษณา</p>'
                '<h3>การยกเลิกและลบข้อมูล</h3><p>กด "ยกเลิกการเชื่อม" ในหน้า Likes เพื่อลบ token ออกจากเซิร์ฟเวอร์ '
                'หรือถอนสิทธิ์ได้ทุกเมื่อที่ <a href="https://myaccount.google.com/permissions">myaccount.google.com/permissions</a></p>'
                '<p>การใช้ข้อมูลที่ได้รับจาก Google API เป็นไปตาม '
                '<a href="https://developers.google.com/terms/api-services-user-data-policy">Google API Services User Data Policy</a> '
                'รวมถึงข้อกำหนด Limited Use</p>'
                '<h3>ติดต่อ</h3><p>spongelotus@gmail.com</p>'
                '<hr style="border:0;border-top:1px solid #ddd;margin:20px 0">'
                '<div lang="en" class="muted"><p><b>Privacy policy (English).</b> spongelotus Hub is a private app used only by its owner. '
                'With the owner\'s consent it requests the read-only <code>youtube.readonly</code> scope to read the owner\'s channel name '
                'and liked videos (titles and links), solely to send the owner phone notifications for sharing them on X manually. '
                'It never posts, edits or deletes anything on YouTube. Tokens and notification history are stored only on the owner\'s '
                'private server and are never sold, shared or used for advertising. Access can be removed at any time with '
                '"Disconnect" on the Likes page or at myaccount.google.com/permissions. Use of information received from Google APIs '
                'adheres to the Google API Services User Data Policy, including the Limited Use requirements. '
                'Contact: spongelotus@gmail.com</p></div>'
                '<div class="actions"><a class="btn gray" href="/about">กลับ</a></div></div>')
        self.send_html(page("นโยบายความเป็นส่วนตัว", body, nav=False, heading="นโยบายความเป็นส่วนตัว"))

    def login_page(self, error=""):
        err = f'<p class="soon" style="margin:0 0 8px">{E(error)}</p>' if error else ""
        body = (f'<div class="card" style="max-width:400px;margin:0 0 40px">{err}'
                f'<form method="post" action="/login"><label style="margin-top:0">รหัสผ่าน</label>'
                f'<input type="password" name="password" autofocus autocomplete="current-password">'
                f'<div class="actions"><button>เข้าสู่ระบบ</button></div></form></div>')
        self.send_html(page("เข้าสู่ระบบ", body, nav=False, heading="เข้าสู่ระบบ"))

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

    def query(self):
        return urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)

    def flash(self, table):
        msg = table.get((self.query().get("msg") or [""])[0], "")
        return f'<div class="flash">{E(msg)}</div>' if msg else ""

    # ---------- recorder ----------
    def home(self):
        cfg = settings.load()
        parts = [self.flash({"checking": "กำลังเช็กทุกบัญชีใหม่… รีเฟรชในอีกสักครู่", "deleted": "ลบแล้ว"})]
        rows = []
        targets = [(u, u, "") for u in cfg["accounts"]] + \
                  [(u, "ig:" + u, ' <span class="pill">IG</span>') for u in cfg["ig_accounts"]]
        n_live = 0
        for user, key, tag in targets:
            st = self.mgr.status.get(key, {}) if self.mgr else {}
            state = st.get("state")
            if state == "recording":
                n_live += 1
                pill = '<span class="pill live">REC</span>'
            elif state == "offline":
                pill = '<span class="pill">offline</span>'
            elif state == "error":
                pill = f'<span class="pill err" title="{E(st.get("error", ""))}">เช็กไม่ได้</span>'
            else:
                pill = '<span class="pill">รอเช็ก</span>'
            checked = datetime.fromtimestamp(st["checked"], record.TZ).strftime("%H:%M") if st.get("checked") else "–"
            err = f'<div class="muted">{E(st.get("error", ""))}</div>' if state == "error" else ""
            rows.append(f'<div class="row"><div><span class="title">@{E(user)}</span>{tag}'
                        f'<div class="muted mono">เช็กล่าสุด {checked}</div>{err}</div>{pill}</div>')
        if not rows:
            rows.append('<p class="muted" style="margin:10px 0">ยังไม่มีบัญชีที่เฝ้า — <a href="/recorder/settings"><u>เพิ่มในหน้าตั้งค่า</u></a></p>')
        du = shutil.disk_usage(settings.DATA_DIR)
        parts.append(f'<h2>บัญชีที่เฝ้า · TikTok ทุก {cfg["check_interval"]} วิ · IG ทุก {cfg["ig_check_interval"]} วิ</h2>'
                     f'<div class="card list">{"".join(rows)}</div><div class="actions bar" style="margin:0 0 10px">'
                     f'<form method="post" action="/check-now"><button class="gray sm">เช็กเดี๋ยวนี้</button></form>'
                     f'<a class="btn gray sm" href="/recorder/settings">ตั้งค่า · เพิ่มบัญชี</a></div>')
        recs = list_recordings()
        parts.append(f"<h2>ไลฟ์ที่อัดไว้ · {len(recs)}</h2>")
        if not recs:
            parts.append('<div class="card note muted">ยังไม่มี — พอมีคนที่เฝ้าไลฟ์ ระบบจะอัดให้อัตโนมัติ</div>')
        for r in recs:
            parts.append(self.rec_card(r))
        sub = (f'{n_live} กำลังอัด · ว่าง {du.free / (1 << 30):.1f} / {du.total / (1 << 30):.0f} GB')
        self.send_html(page("อัดไลฟ์", "".join(parts), active="/recorder", heading="อัดไลฟ์", sub=sub))

    def rec_card(self, r):
        status = r.get("status")
        ig = ' <span class="pill">IG</span>' if r.get("platform") == "instagram" else ""
        head = (f'<span class="title">@{E(r.get("user", ""))}</span>{ig} <span class="muted">{E(r.get("nickname") or "")}</span>'
                f'<div class="muted mono">{thai_time(r.get("started"))} – '
                f'{thai_time(r["ended"]) if r.get("ended") else "…"}</div>'
                f'<div class="muted">{E(r.get("title") or "")}</div>')
        if status == "recording":
            return (f'<div class="card">{head}<div class="actions" style="margin-top:10px"><span class="pill live">REC</span>'
                    f'<span class="muted">{r.get("segments", 0)} ท่อน — ไฟล์จะพร้อมหลังไลฟ์จบ</span></div></div>')
        if status == "merging":
            return f'<div class="card">{head}<p class="muted">กำลังรวมไฟล์…</p></div>'
        body = []
        for f in r.get("files", []):
            url = "/files/" + urllib.parse.quote(f'{r["_path"]}/{f["name"]}')
            info = (f'{record.fmt_duration(f.get("duration", 0))} · {record.fmt_size(f["size"])}'
                    f'{" · %sx%s" % (f["width"], f["height"]) if f.get("width") else ""}')
            player = (f'<details><summary>ดูเลย</summary><video controls preload="none" playsinline '
                      f'src="{url}"></video></details>') if f["name"].endswith(".mp4") else ""
            body.append(f'<div style="margin-top:14px"><div class="row plain"><div class="mono" style="font-size:13px">'
                        f'{E(f["name"])}<div class="muted">{info}</div></div>'
                        f'<a class="btn gray sm" href="{url}?dl=1">โหลด</a></div>{player}</div>')
        if status == "failed" and not body:
            body.append('<p class="muted">อัดไม่ได้ — ดูสาเหตุในหน้า "บันทึก"</p>')
        delete = (f'<form method="post" action="/delete" onsubmit="return confirm(\'ลบไลฟ์นี้ถาวร?\')">'
                  f'<input type="hidden" name="path" value="{E(r["_path"])}">'
                  f'<button class="red sm">ลบ</button></form>')
        return (f'<div class="card"><div class="row plain" style="align-items:flex-start">'
                f'<div>{head}</div>{delete}</div>{"".join(body)}</div>')

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
        n_recs = len(list_recordings())
        data = subs.load()
        month, year = subs.totals(data)
        up = subs.upcoming(data, 30)
        ld = likes.load()
        pend = [it for it in ld["items"] if it.get("status") == "pending"]

        now = datetime.now(record.TZ)
        greet = "อรุณสวัสดิ์" if now.hour < 12 else "สวัสดีตอนบ่าย" if now.hour < 18 else "สวัสดีตอนเย็น"
        nxt = (f'{E(up[0][1]["name"])} {"วันนี้" if up[0][0] == 0 else f"อีก {up[0][0]} วัน"}' if up else "ไม่มีใน 30 วัน")
        summary = (f'<div class="card dark wide"><p class="hello">{greet}, Bua</p>'
                   f'<div class="muted">{now.strftime("%A %d %B %Y · %H:%M")}</div>'
                   f'<div class="stats" style="margin-top:10px">'
                   f'<div class="stat"><b>{len(live)}</b><span>กำลังอัดไลฟ์</span></div>'
                   f'<div class="stat"><b>{len(pend)}</b><span>คลิปรอโพสต์ X</span></div>'
                   f'<div class="stat"><b>{subs.fmt_money(month)}</b><span>subscriptions / เดือน</span></div>'
                   f'<div class="stat"><b class="clip" style="font-size:15px;padding:3px 0">{nxt}</b><span>ตัดบัตรครั้งถัดไป</span></div>'
                   f'</div></div>')

        pill = (f'<span class="pill live">REC {len(live)}</span>' if live else '<span class="pill">idle</span>')
        who = f'<div class="title">{E(", ".join(live))}</div>' if live else ""
        err_line = f'<div class="muted soon">เช็กไม่ได้ {errs} บัญชี</div>' if errs else ""
        rec_card = (f'<a class="card k-rec" href="/recorder"><div class="chead"><div class="ctitle">'
                    f'<span class="ico">{icon("rec")}</span><span>อัดไลฟ์<small>TikTok · Instagram</small></span></div>{pill}</div>'
                    f'<div class="big">{len(live)}<small>กำลังอัด</small></div>{who}'
                    f'<div class="row"><span>เฝ้าอยู่</span><b>{n_acc} บัญชี</b></div>'
                    f'<div class="row"><span>ไลฟ์ที่เก็บไว้</span><b>{n_recs}</b></div>'
                    f'<div class="row"><span>พื้นที่ว่าง</span><b>{du.free / (1 << 30):.0f} GB · {free_pct:.0f}%</b></div>{err_line}</a>')

        lines = []
        for n, it in up[:3]:
            when = "วันนี้" if n == 0 else f"{n} วัน"
            cls = ' class="soon"' if n <= int(data.get("remind_days") or 3) else ""
            lines.append(f'<div class="row"><span>{E(it["name"])}</span><b{cls}>{when}</b></div>')
        up_html = "".join(lines) or '<div class="row"><span class="muted">30 วันข้างหน้าไม่มีตัดบัตร</span></div>'
        n_cancel = sum(1 for it in data["items"] if it.get("status") == "cancelling")
        cancel = f'<span class="pill err">รอยกเลิก {n_cancel}</span>' if n_cancel else ""
        subs_card = (f'<a class="card k-subs" href="/subs"><div class="chead"><div class="ctitle">'
                     f'<span class="ico">{icon("card")}</span><span>Subscriptions<small>≈ {subs.fmt_money(year)} / ปี</small></span></div>{cancel}</div>'
                     f'<div class="big">{subs.fmt_money(month)}<small>/ เดือน</small></div>{up_html}</a>')

        if likes.connected(ld):
            last = ld["items"][0]["title"] if ld["items"] else "ยังไม่มีคลิปใหม่"
            lpill = '<span class="pill err">มีปัญหา</span>' if ld.get("last_error") else '<span class="pill ok">on</span>'
            likes_main = (f'<div class="big">{len(pend)}<small>รอโพสต์</small></div>'
                          f'<div class="row" style="display:block"><div class="muted">ล่าสุด</div><div class="clip">{E(last)}</div></div>')
        else:
            lpill = '<span class="pill">ยังไม่เชื่อม</span>'
            likes_main = ('<div class="row" style="display:block">กดไลค์คลิป YouTube แล้วเด้งแจ้งเตือน'
                          'ให้แตะโพสต์ลง X</div><div class="actions"><span class="btn acc sm">ตั้งค่าเลย</span></div>')
        likes_card = (f'<a class="card k-likes" href="/likes"><div class="chead"><div class="ctitle">'
                      f'<span class="ico">{icon("heart")}</span><span>Likes<small>YouTube → X</small></span></div>{lpill}</div>'
                      f'{likes_main}</a>')
        body = f'<div class="bento">{summary}{rec_card}{likes_card}{subs_card}</div>'
        self.send_html(page("Hub", body, active="/", heading=None))

    # ---------- subscriptions ----------
    def subs_page(self):
        data = subs.load()
        edit_id = (self.query().get("edit") or [""])[0]
        rate = float(data.get("usd_rate") or 33)
        month, year = subs.totals(data)
        parts = [self.flash({"saved": "บันทึกแล้ว", "deleted": "ลบแล้ว"})]
        parts.append(f'<div class="card dark sumline"><b>{subs.fmt_money(month)}<small>/ เดือน</small></b>'
                     f'<b>{subs.fmt_money(year)}<small>/ ปี</small></b>'
                     f'<span class="muted mono" title="นับเฉพาะรายเดือน/รายปีที่ยังไม่ยกเลิก">1 USD = {rate:g} THB</span></div>')
        if any(it.get("mock") for it in data["items"]):
            parts.append('<div class="card note muted" style="padding:9px 16px">ป้าย <span class="pill mock">ตัวอย่าง</span> '
                         '= ราคา/วันที่ยังไม่จริง แตะรายการเพื่อแก้</div>')
        for status in ("cancelling", "active", "cancelled"):
            group = [it for it in data["items"] if it.get("status") == status]
            if not group:
                continue
            group.sort(key=lambda it: (subs.days_left(it) if subs.days_left(it) is not None else 9999, it.get("name", "")))
            rows = "".join(self.sub_row(it, rate, data) for it in group)
            block = f'<div class="card list">{rows}</div>'
            if status == "cancelled":
                parts.append(f'<details class="fold"><summary>{subs.STATUSES[status]} · {len(group)}</summary>{block}</details>')
            else:
                parts.append(f'<h2>{subs.STATUSES[status]} · {len(group)}</h2>{block}')
        editing = next((it for it in data["items"] if it["id"] == edit_id), None)
        title = "แก้ไข · " + E(editing["name"]) if editing else "+ เพิ่มรายการ"
        parts.append(f'<details class="fold" id="form"{" open" if editing else ""}><summary>{title}</summary>'
                     f'{self.sub_form(editing)}</details>')
        parts.append(f'''<details class="fold"><summary>ตั้งค่า</summary><form method="post" action="/subs/options" class="card">
<div class="two"><div><label style="margin-top:0">เรต USD → บาท</label><input name="usd_rate" type="number" step="0.01" min="1" value="{rate:g}"></div>
<div><label style="margin-top:0">เตือนก่อนตัดบัตร (วัน)</label><input name="remind_days" type="number" min="0" max="30"
 value="{int(data.get("remind_days") or 3)}"></div></div>
<div class="actions"><button>บันทึก</button></div>
<p class="muted" style="margin:14px 0 0">แจ้งเตือนส่งผ่าน ntfy ตัวเดียวกับระบบอัดไลฟ์ · เลยวันต่ออายุแล้ว ระบบเลื่อนไปรอบถัดไปให้เอง
(เฉพาะ "ใช้อยู่")</p></form></details>''')
        n = sum(1 for it in data["items"] if it.get("status") != "cancelled")
        self.send_html(page("Subscriptions", "".join(parts), active="/subs", heading="Subscriptions",
                            sub=f"{n} รายการ"))

    def sub_row(self, it, rate, data):
        thb = subs.to_thb(it, rate)
        cycle = subs.CYCLES.get(it.get("cycle"), "")
        if it.get("cycle") == "free":
            price, per = "ฟรี", ""
        elif not it.get("price"):
            price, per = "฿0", cycle
        elif it.get("currency") == "USD":
            price, per = f'${float(it["price"]):,.2f}', f'≈ {subs.fmt_money(thb)} · {cycle}'
        else:
            price, per = subs.fmt_money(float(it["price"])), cycle
        n = subs.days_left(it)
        when = ""
        if n is not None:
            if n < 0:
                when = f' · เลยมา {-n} วัน'
            else:
                soon = n <= int(data.get("remind_days") or 3)
                txt = "ต่ออายุวันนี้" if n == 0 else f"อีก {n} วัน"
                when = f' · <span class="{"soon" if soon else ""}">{txt}</span>'
        tags = ' <span class="pill mock">ตัวอย่าง</span>' if it.get("mock") else ""
        if it.get("category"):
            tags += f' <span class="pill">{E(it["category"])}</span>'
        note = f' · {E(it["note"])}' if it.get("note") else ""
        dim = " dim" if it.get("status") == "cancelled" else ""
        date = f'<small>{E(it["next"])}</small>' if it.get("next") and it.get("status") != "cancelled" else ""
        return (f'<a class="row{dim}" href="/subs?edit={E(it["id"])}#form" title="แตะเพื่อแก้"><div style="min-width:0">'
                f'<span class="title">{E(it["name"])}</span>{tags}'
                f'<div class="muted line2">{(per + when + note).removeprefix(" · ")}</div></div>'
                f'<div class="price">{price}{date}</div></a>')

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
            delete = (f'<form method="post" action="/subs/delete" onsubmit="return confirm(\'ลบรายการนี้?\')" '
                      f'style="margin-top:8px"><input type="hidden" name="id" value="{E(it["id"])}">'
                      f'<button class="red sm">ลบรายการนี้</button></form>')
        cancel = '<a class="btn gray" href="/subs">ยกเลิก</a>' if it["id"] else ""
        return f'''<div class="card"><form method="post" action="/subs/save">
<input type="hidden" name="id" value="{E(it["id"])}">
<label style="margin-top:0">ชื่อบริการ</label><input name="name" required value="{E(it["name"])}" placeholder="เช่น Netflix">
<div class="two"><div><label>ราคา</label><input name="price" type="number" step="0.01" min="0" value="{price}"></div>
<div><label>สกุลเงิน</label><select name="currency">{opts(cur, it.get("currency"))}</select></div>
<div><label>รอบจ่าย</label><select name="cycle">{opts(subs.CYCLES, it.get("cycle"))}</select></div>
<div><label>ต่ออายุครั้งถัดไป</label><input name="next" type="date" value="{E(it.get("next", ""))}"></div>
<div><label>หมวด</label><input name="category" value="{E(it.get("category", ""))}" placeholder="เช่น AI"></div>
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

    # ---------- likes ----------
    def likes_page(self):
        d = likes.load()
        parts = [self.flash({"saved": "บันทึกแล้ว", "checking": "กำลังเช็ก… รีเฟรชในอีกสักครู่",
                             "disconnected": "ยกเลิกการเชื่อมแล้ว", "err": "เชื่อมไม่สำเร็จ — ดูข้อความด้านล่าง"})]
        dev = d.get("device") or {}
        refresh = ""
        if likes.connected(d):
            acct = f' · {E(d["account"])}' if d.get("account") else ""
            status = ('<span class="pill err">มีปัญหา</span>' if d.get("last_error") else
                      '<span class="pill ok">ทำงานอยู่</span>' if d.get("enabled") else '<span class="pill">ปิดอยู่</span>')
            last = (datetime.fromtimestamp(d["last_check"], record.TZ).strftime("%H:%M") if d.get("last_check") else "–")
            err = f'<p class="soon" style="margin:8px 0 0">{E(d["last_error"])}</p>' if d.get("last_error") else ""
            parts.append(f'<div class="card"><div class="row plain"><div>'
                         f'<span class="title">เชื่อม YouTube แล้ว</span><span class="muted">{acct}</span>'
                         f'<div class="muted mono">เช็กล่าสุด {last} · ทุก {int(d.get("interval_min") or 10)} นาที</div></div>'
                         f'{status}</div>{err}<div class="actions"><form method="post" action="/likes/check-now">'
                         f'<button class="gray sm">เช็กเดี๋ยวนี้</button></form></div></div>')
            pend = [it for it in d["items"] if it.get("status") == "pending"]
            rest = [it for it in d["items"] if it.get("status") != "pending"][:30]
            parts.append(f'<h2>รอโพสต์ · {len(pend)}</h2>')
            if pend:
                parts.append('<div class="card list">' + "".join(self.like_row(d, it) for it in pend) + '</div>')
            else:
                parts.append('<div class="card note muted">ไม่มีคลิปค้างโพสต์ — กดไลค์คลิปใหม่บน YouTube แล้วจะเด้งแจ้งเตือนใน ntfy '
                             '(ภายใน ~{} นาที)</div>'.format(int(d.get("interval_min") or 10)))
            if rest:
                parts.append(f'<details class="fold"><summary>ก่อนหน้านี้ · {len(rest)}</summary><div class="card list">' + "".join(self.like_row(d, it) for it in rest) + '</div></details>')
            parts.append(f'''<details class="fold"><summary>ตั้งค่า</summary><form method="post" action="/likes/options" class="card">
<label style="margin-top:0"><input type="checkbox" name="enabled" value="1"{" checked" if d.get("enabled") else ""}>เปิดใช้งาน</label>
<label>เช็กทุกกี่นาที (ต่ำสุด 5)</label><input name="interval_min" type="number" min="5" value="{int(d.get("interval_min") or 10)}">
<label>ข้อความทวีต · ใช้ <code>{{title}}</code> แทนชื่อคลิป, <code>{{channel}}</code> แทนชื่อช่อง · ลิงก์คลิปต่อท้ายให้เอง</label>
<input name="template" value="{E(d.get("template") or likes.DEFAULT_TEMPLATE)}">
<div class="actions"><button>บันทึก</button></div></form>
<form method="post" action="/likes/disconnect" onsubmit="return confirm('ยกเลิกการเชื่อม YouTube?')" class="actions">
<button class="red sm">ยกเลิกการเชื่อม YouTube</button></form></details>''')
        elif dev.get("user_code") and not dev.get("error"):
            refresh = '<meta http-equiv="refresh" content="5">'
            parts.append(f'''<div class="card"><h2>ขั้นสุดท้าย · ยืนยันกับ Google</h2>
<ol class="steps"><li>เปิด <a href="{E(dev["url"])}" target="_blank" rel="noopener"><u>{E(dev["url"])}</u></a> (มือถือหรือคอมก็ได้)</li>
<li>ใส่รหัสนี้</li></ol><div class="code-big">{E(dev["user_code"])}</div>
<ol class="steps" start="3"><li>เลือกบัญชี YouTube ที่ใช้กดไลค์</li>
<li>ถ้าขึ้น “Google hasn’t verified this app” กด <b>Advanced</b> → <b>Go to … (unsafe)</b> → <b>Continue</b> (เป็นแอปของเราเอง ไม่เป็นไร)</li></ol>
<p class="muted">หน้านี้จะรีเฟรชเองเมื่อเชื่อมสำเร็จ</p>
<div class="actions"><form method="post" action="/likes/connect"><button class="gray sm">ขอรหัสใหม่</button></form>
<form method="post" action="/likes/cancel"><button class="gray sm">ยกเลิก</button></form></div></div>''')
        else:
            if dev.get("error"):
                parts.append(f'<div class="flash" style="background:#ffe0d8;color:#b2321d">{E(dev["error"])}</div>')
            has = bool(d.get("client_id") and d.get("client_secret"))
            parts.append(f'''<div class="card note"><h2>ทำอะไร</h2>
<p style="margin:0">กดไลค์คลิปบน YouTube → ภายในไม่กี่นาที ntfy เด้งแจ้งเตือน → แตะ → X เปิดพร้อมข้อความ
“Liked on YouTube: ชื่อคลิป” + ลิงก์ → กด Post · ทวีตแยกทีละคลิป ฟรี ไม่ต้องใช้ IFTTT</p></div>
<h2>ตั้งค่าครั้งเดียว · ประมาณ 10 นาที</h2><div class="card"><ol class="steps">
<li>เข้า <a href="https://console.cloud.google.com/" target="_blank" rel="noopener"><u>console.cloud.google.com</u></a>
 ด้วยบัญชี Google เดียวกับ YouTube → สร้างโปรเจกต์ใหม่ (ชื่ออะไรก็ได้ เช่น <code>hub</code>)</li>
<li>ค้นหา <b>YouTube Data API v3</b> ในช่องค้นหาด้านบน → กด <b>Enable</b></li>
<li>ไปที่ <b>OAuth consent screen</b> (บางทีชื่อ <b>Google Auth Platform</b>) → ตั้งค่าแบบ <b>External</b>
 ใส่ชื่อแอปกับอีเมลตัวเอง</li>
<li>ในส่วน <b>Audience</b> กด <b>Publish app</b> ให้เป็น <b>In production</b>
 <span class="muted">— ถ้าค้างไว้ที่ Testing จะต้องเชื่อมใหม่ทุก 7 วัน</span></li>
<li>ไปที่ <b>Clients</b> (หรือ <b>Credentials</b>) → <b>Create client</b> → ประเภท
 <b>TVs and Limited Input devices</b> → จะได้ <b>Client ID</b> กับ <b>Client secret</b> มาวางข้างล่าง</li></ol>
<form method="post" action="/likes/creds"><label>Client ID</label>
<input name="client_id" value="{E(d.get("client_id", ""))}" placeholder="xxxx.apps.googleusercontent.com" autocomplete="off">
<label>Client secret</label><input name="client_secret" type="password"
 placeholder="{"บันทึกไว้แล้ว — เว้นว่างได้" if d.get("client_secret") else "GOCSPX-..."}" autocomplete="off">
<div class="actions"><button class="gray">บันทึก</button></div></form>
<form method="post" action="/likes/connect" class="actions" style="margin-top:20px">
<button{"" if has else " disabled style=opacity:.4"}>เชื่อม YouTube</button>
<span class="muted">{"" if has else "ใส่ Client ID / secret ก่อน"}</span></form></div>''')
        n = sum(1 for it in d["items"] if it.get("status") == "pending")
        html_ = page("Likes", "".join(parts), active="/likes", heading="Likes",
                     sub=f"YouTube → X · {n} รอโพสต์" if likes.connected(d) else "YouTube → X")
        if refresh:
            html_ = html_.replace("<head>", "<head>" + refresh, 1)
        self.send_html(html_)

    def like_row(self, d, it):
        when = ""
        if it.get("found"):
            when = datetime.fromtimestamp(it["found"], record.TZ).strftime("%d/%m %H:%M")
        status = it.get("status")
        right = ""
        if status == "pending":
            right = (f'<a class="btn sm" href="/likes/go?id={urllib.parse.quote(it["id"])}" target="_blank" rel="noopener">โพสต์</a>'
                     f'<form method="post" action="/likes/mark" style="display:inline"><input type="hidden" name="id" value="{E(it["id"])}">'
                     f'<input type="hidden" name="status" value="skipped"><button class="gray sm">ข้าม</button></form>')
        else:
            label = "โพสต์แล้ว" if status == "posted" else "ข้าม"
            right = (f'<span class="pill{" ok" if status == "posted" else ""}">{label}</span>'
                     f'<form method="post" action="/likes/mark" style="display:inline"><input type="hidden" name="id" value="{E(it["id"])}">'
                     f'<input type="hidden" name="status" value="pending"><button class="gray sm">ย้อนกลับ</button></form>')
        ch = f'{E(it.get("channel", ""))} · ' if it.get("channel") else ""
        return (f'<div class="row" style="align-items:flex-start"><div style="min-width:0">'
                f'<a href="{E(likes.video_url(it["id"]))}" target="_blank" rel="noopener" class="title" '
                f'style="font-size:15px">{E(it["title"])}</a><div class="muted">{ch}<span class="mono">{when}</span></div></div>'
                f'<div class="actions" style="margin:0;flex:none;justify-content:flex-end">{right}</div></div>')

    def likes_go(self, need_token=False):
        q = self.query()
        vid = (q.get("id") or [""])[0]
        d = likes.load()
        it = next((x for x in d["items"] if x["id"] == vid), None)
        if need_token and not (it and it.get("tok") and
                               hmac.compare_digest(it["tok"], (q.get("t") or [""])[0])):
            return self.redirect("/login")
        if not it:
            return self.redirect("/likes")
        likes.mark(vid, "posted")
        self.redirect(likes.intent_url(d, it))

    def likes_post(self, path, f):
        if path == "/likes/creds":
            def fn(d):
                cid = f.get("client_id", "").strip()
                if cid != d.get("client_id"):
                    d.update(refresh_token="", access_token="", expires=0)
                d["client_id"] = cid
                if f.get("client_secret", "").strip():
                    d["client_secret"] = f["client_secret"].strip()
            likes.update(fn)
            return self.redirect("/likes?msg=saved")
        if path == "/likes/connect":
            try:
                likes.start_device_flow()
            except Exception as e:
                likes.update(lambda d: d.update(device={"error": f"เริ่มเชื่อมไม่ได้: {e}"}))
            return self.redirect("/likes")
        if path == "/likes/cancel":
            likes.cancel_device_flow()
            return self.redirect("/likes")
        if path == "/likes/disconnect":
            likes.disconnect()
            return self.redirect("/likes?msg=disconnected")
        if path == "/likes/check-now":
            threading.Thread(target=likes.check, kwargs={"force": True}, daemon=True).start()
            time.sleep(2)
            return self.redirect("/likes?msg=checking")
        if path == "/likes/options":
            def fn(d):
                d["enabled"] = f.get("enabled") == "1"
                try:
                    d["interval_min"] = max(5, int(f.get("interval_min") or 10))
                except ValueError:
                    pass
                d["template"] = (f.get("template") or likes.DEFAULT_TEMPLATE).strip()[:200]
            likes.update(fn)
            return self.redirect("/likes?msg=saved")
        if path == "/likes/mark":
            if f.get("status") in ("pending", "posted", "skipped"):
                likes.mark(f.get("id", ""), f["status"])
            return self.redirect("/likes")
        self.send_html("not found", 404)

    def settings_msg(self):
        return self.flash({"saved": "บันทึกแล้ว ✅", "sent": "ส่งแจ้งเตือนทดสอบแล้ว — ดูในแอป ntfy",
                           "fail": "ส่งแจ้งเตือนไม่สำเร็จ", "pw": "รหัสผ่านใหม่ต้องยาวอย่างน้อย 8 ตัว"})

    # ---------- recorder settings (/recorder/settings) ----------
    def rec_settings_page(self):
        cfg = settings.load()
        body = f"""{self.settings_msg()}
<h2>บัญชีที่เฝ้า · การอัด</h2><form method="post" action="/recorder/settings" class="card">
<label style="margin-top:0">บัญชี TikTok ที่จะเฝ้า (บรรทัดละ 1 ชื่อ จะมี @ หรือวางลิงก์โปรไฟล์ก็ได้)</label>
<textarea name="accounts" rows="6" placeholder="เช่น&#10;@username1&#10;username2">{E(chr(10).join(cfg["accounts"]))}</textarea>
<label>เช็กทุกกี่วินาที (ต่ำสุด 20 · แนะนำ 60)</label>
<input name="check_interval" type="number" min="20" value="{cfg["check_interval"]}">
<label>ลบไลฟ์ที่เก่ากว่ากี่วัน (0 = ไม่ลบเลย)</label>
<input name="retention_days" type="number" min="0" value="{cfg.get("retention_days", 0)}">
<label>ถ้าดิสก์ว่างน้อยกว่ากี่ GB ให้ลบไลฟ์เก่าสุดอัตโนมัติ</label>
<input name="min_free_gb" type="number" min="0" value="{cfg.get("min_free_gb", 10)}">
<label>บัญชี Instagram ที่จะเฝ้า (บรรทัดละ 1 ชื่อ) · บัญชีสำรองที่ใช้คุกกี้ต้อง<b>กดติดตาม</b>ทุกบัญชีที่จะเฝ้า</label>
<textarea name="ig_accounts" rows="4" placeholder="เช่น&#10;@username1">{E(chr(10).join(cfg["ig_accounts"]))}</textarea>
<label>คุกกี้ Instagram (จำเป็นสำหรับ IG — ใช้บัญชีสำรอง)</label>
<textarea name="ig_cookies" rows="3" placeholder="sessionid=...; ds_user_id=...; csrftoken=...">{E(cfg.get("ig_cookies", ""))}</textarea>
<label>เช็ก IG ทุกกี่วินาที (ต่ำสุด 45 · แนะนำ 90)</label>
<input name="ig_check_interval" type="number" min="45" value="{cfg["ig_check_interval"]}">
<label>คุกกี้ TikTok (ไม่บังคับ — ใส่เมื่อเช็กไม่ได้หรือเจอไลฟ์ 18+)</label>
<textarea name="cookies" rows="3" placeholder="sessionid=...; tt-target-idc=...">{E(cfg.get("cookies", ""))}</textarea>
<div class="actions"><button>บันทึก</button><a class="btn gray" href="/recorder">กลับหน้าอัดไลฟ์</a></div></form>
"""
        self.send_html(page("ตั้งค่าอัดไลฟ์", body, active="/recorder", heading="ตั้งค่าอัดไลฟ์"))

    def rec_settings_post(self, f):
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
        cfg.update({
            "accounts": accounts,
            "ig_accounts": ig_accounts,
            "ig_cookies": f.get("ig_cookies", "").strip(),
            "ig_check_interval": max(45, int(f.get("ig_check_interval") or 90)),
            "check_interval": max(20, int(f.get("check_interval") or 60)),
            "retention_days": max(0, int(f.get("retention_days") or 0)),
            "min_free_gb": max(0, float(f.get("min_free_gb") or 0)),
            "cookies": f.get("cookies", "").strip(),
        })
        settings.save(cfg)
        self.redirect("/recorder/settings?msg=saved")

    # ---------- hub-wide settings (/settings): password + ntfy ----------
    def settings_page(self):
        cfg = settings.load()
        topic = cfg.get("ntfy_topic", "")
        body = f"""{self.settings_msg()}
<h2>รหัสผ่านหน้าเว็บ</h2><form method="post" action="/settings" class="card">
<label style="margin-top:0">รหัสผ่านใหม่ (อย่างน้อย 8 ตัว) · ใช้เข้าทุกส่วนของ Hub</label>
<input name="new_password" type="password" autocomplete="new-password">
<div class="actions"><button>เปลี่ยนรหัสผ่าน</button></div></form>
<h2>แจ้งเตือน · ntfy</h2><div class="card">
<p style="margin-top:0">ในแอป ntfy กด <b>+</b> แล้วใส่ชื่อ topic นี้ · ใช้ร่วมกันทั้งอัดไลฟ์ Subscriptions และ Likes</p>
<p><code style="font-size:16px">{E(topic)}</code></p>
<p class="muted">ใครรู้ชื่อนี้ก็เห็นแจ้งเตือนได้ อย่าแชร์ต่อ</p>
<form method="post" action="/test-notify" class="actions"><button class="gray">ส่งแจ้งเตือนทดสอบ</button></form></div>
<h2>ตั้งค่าแต่ละส่วน</h2><div class="card">
<div class="actions" style="margin-top:0"><a class="btn gray" href="/recorder/settings">อัดไลฟ์</a>
<a class="btn gray" href="/subs">Subscriptions</a><a class="btn gray" href="/likes">Likes</a></div></div>
"""
        self.send_html(page("ตั้งค่าระบบ", body, active="/settings", heading="ตั้งค่าระบบ"))

    def settings_post(self, f):
        cfg = settings.load()
        new_pw = f.get("new_password", "")
        if len(new_pw) < 8:
            return self.redirect("/settings?msg=pw")
        cfg["web_password"] = new_pw
        settings.save(cfg)
        # old sessions become invalid; give this browser a fresh one
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
        self.send_html(page("บันทึกระบบ", '<h2>ล่าสุดอยู่บน</h2>' + body, active="/log", heading="บันทึกระบบ"))

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
