"""YouTube likes → ntfy → post on X yourself (free, no X API).

Every few minutes: read the "Liked videos" playlist (LL) of the connected YouTube
account. For each new like, send an ntfy notification whose tap opens X with the
post already written ("Liked on YouTube: <title>" + link). One tweet per clip.

Google sign-in uses the OAuth "device" flow (client type "TVs and Limited Input
devices"), so no redirect URL / domain is needed.

Data (incl. tokens) lives on the server only: /var/lib/live-recorder/likes.json
"""
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from . import notify, settings

log = logging.getLogger("likes")

LIKES_FILE = settings.DATA_DIR / "likes.json"
SCOPE = "https://www.googleapis.com/auth/youtube.readonly"
DEVICE_URL = "https://oauth2.googleapis.com/device/code"
TOKEN_URL = "https://oauth2.googleapis.com/token"
API = "https://www.googleapis.com/youtube/v3/"
DEFAULT_TEMPLATE = "Liked on YouTube: {title}"
MAX_ITEMS = 150
MAX_SEEN = 600

DEFAULTS = {
    "client_id": "", "client_secret": "", "refresh_token": "", "access_token": "", "expires": 0,
    "account": "", "enabled": True, "interval_min": 10, "template": DEFAULT_TEMPLATE,
    "initialized": False, "seen": [], "items": [], "device": {}, "last_check": 0,
    "last_error": "", "auth_error_notified": 0,
}

_lock = threading.Lock()
_check_lock = threading.Lock()


def load():
    with _lock:
        data = dict(DEFAULTS)
        if LIKES_FILE.exists():
            try:
                data.update(json.loads(LIKES_FILE.read_text("utf-8")))
            except Exception:
                log.exception("likes.json unreadable")
        return data


def save(data):
    with _lock:
        settings.DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = LIKES_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")
        os.chmod(tmp, 0o600)
        tmp.replace(LIKES_FILE)


def update(fn):
    """Load → change → save under one lock-free read/modify/write (callers are few)."""
    data = load()
    fn(data)
    save(data)
    return data


def connected(data=None):
    return bool((data or load()).get("refresh_token"))


# ---------- HTTP ----------
class ApiError(Exception):
    def __init__(self, msg, code=None, reason=""):
        super().__init__(msg)
        self.code, self.reason = code, reason


def _post(url, fields):
    req = urllib.request.Request(url, data=urllib.parse.urlencode(fields).encode(),
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        return json.loads(urllib.request.urlopen(req, timeout=20).read())
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read())
        except Exception:
            body = {}
        raise ApiError(body.get("error_description") or body.get("error") or str(e), e.code,
                       body.get("error", ""))


def _get(path, params, token):
    url = API + path + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        return json.loads(urllib.request.urlopen(req, timeout=20).read())
    except urllib.error.HTTPError as e:
        try:
            err = json.loads(e.read()).get("error", {})
        except Exception:
            err = {}
        reason = ((err.get("errors") or [{}])[0]).get("reason", "")
        raise ApiError(err.get("message") or str(e), e.code, reason)


# ---------- sign-in (device flow) ----------
def start_device_flow():
    data = load()
    if not data["client_id"] or not data["client_secret"]:
        raise ApiError("ยังไม่ได้ใส่ Client ID / Client secret")
    r = _post(DEVICE_URL, {"client_id": data["client_id"], "scope": SCOPE})
    device = {"device_code": r["device_code"], "user_code": r["user_code"],
              "url": r.get("verification_url") or r.get("verification_uri") or "https://www.google.com/device",
              "expires": time.time() + int(r.get("expires_in", 1800)),
              "interval": max(5, int(r.get("interval", 5))), "error": ""}
    update(lambda d: d.update(device=device))
    threading.Thread(target=_poll_device, args=(device["device_code"],), daemon=True, name="yt-device").start()
    return device


def _poll_device(device_code):
    while True:
        data = load()
        dev = data.get("device") or {}
        if dev.get("device_code") != device_code:
            return  # restarted or cancelled
        if time.time() > dev.get("expires", 0):
            dev["error"] = "รหัสหมดอายุแล้ว กด 'เชื่อม YouTube' ใหม่"
            update(lambda d: d.update(device=dev))
            return
        time.sleep(dev.get("interval", 5))
        try:
            tok = _post(TOKEN_URL, {"client_id": data["client_id"], "client_secret": data["client_secret"],
                                    "device_code": device_code,
                                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code"})
        except ApiError as e:
            if e.reason == "authorization_pending":
                continue
            if e.reason == "slow_down":
                dev["interval"] = dev.get("interval", 5) + 5
                update(lambda d: d.update(device=dev))
                continue
            dev["error"] = f"เชื่อมไม่สำเร็จ: {e}"
            update(lambda d: d.update(device=dev))
            return

        def done(d):
            d.update(refresh_token=tok.get("refresh_token", ""), access_token=tok["access_token"],
                     expires=time.time() + int(tok.get("expires_in", 3600)) - 60, device={},
                     last_error="", initialized=False, seen=[], last_check=0)
        update(done)
        log.info("YouTube connected")
        try:
            ch = _get("channels", {"part": "snippet", "mine": "true"}, tok["access_token"])
            name = ((ch.get("items") or [{}])[0].get("snippet") or {}).get("title", "")
            update(lambda d: d.update(account=name))
        except Exception:
            pass
        threading.Thread(target=check, kwargs={"force": True}, daemon=True).start()
        return


def disconnect():
    update(lambda d: d.update(refresh_token="", access_token="", expires=0, account="", device={},
                              initialized=False, seen=[], last_error=""))


def access_token(data):
    if data["access_token"] and time.time() < data["expires"]:
        return data["access_token"]
    tok = _post(TOKEN_URL, {"client_id": data["client_id"], "client_secret": data["client_secret"],
                            "refresh_token": data["refresh_token"], "grant_type": "refresh_token"})
    data["access_token"] = tok["access_token"]
    data["expires"] = time.time() + int(tok.get("expires_in", 3600)) - 60
    return data["access_token"]


# ---------- checking ----------
def video_url(vid):
    return f"https://www.youtube.com/watch?v={vid}"


def post_text(data, item):
    tpl = data.get("template") or DEFAULT_TEMPLATE
    try:
        return tpl.format(title=item["title"], channel=item.get("channel", ""))
    except Exception:
        return DEFAULT_TEMPLATE.format(title=item["title"])


def intent_url(data, item):
    q = urllib.parse.urlencode({"text": post_text(data, item), "url": video_url(item["id"])})
    return "https://x.com/intent/post?" + q


def fetch_liked(token):
    """Newest likes first: [{id, title, channel, liked_at}]"""
    try:
        r = _get("playlistItems", {"part": "snippet,contentDetails", "playlistId": "LL", "maxResults": 25}, token)
        out = []
        for it in r.get("items", []):
            sn = it.get("snippet") or {}
            vid = (it.get("contentDetails") or {}).get("videoId") or (sn.get("resourceId") or {}).get("videoId")
            if not vid or sn.get("title") in ("Private video", "Deleted video"):
                continue
            out.append({"id": vid, "title": sn.get("title", ""),
                        "channel": sn.get("videoOwnerChannelTitle", ""), "liked_at": sn.get("publishedAt", "")})
        return out
    except ApiError as e:
        if e.code not in (403, 404):
            raise
    r = _get("videos", {"part": "snippet", "myRating": "like", "maxResults": 25}, token)
    return [{"id": v["id"], "title": v["snippet"].get("title", ""),
             "channel": v["snippet"].get("channelTitle", ""), "liked_at": ""} for v in r.get("items", [])]


def check(force=False):
    if not _check_lock.acquire(blocking=False):
        return
    try:
        _check(force)
    finally:
        _check_lock.release()


def _check(force):
    snap = load()
    if not connected(snap) or not (snap.get("enabled") or force):
        return
    if not force and time.time() - snap.get("last_check", 0) < max(5, int(snap.get("interval_min") or 10)) * 60:
        return
    err, liked = "", None
    try:
        token = access_token(snap)
        liked = fetch_liked(token)
    except ApiError as e:
        err = str(e)
        if e.reason == "invalid_grant" and time.time() - snap.get("auth_error_notified", 0) > 86400:
            url = (settings.load().get("public_url") or "").rstrip("/")
            notify.send("⚠️ YouTube หลุดการเชื่อมต่อ", "เข้าไปที่หน้า Likes แล้วกด 'เชื่อม YouTube' ใหม่",
                        tags=["warning"], click=(url + "/likes") if url else None, priority=4)
            snap["auth_error_notified"] = time.time()
    except Exception as e:  # network etc.
        err = str(e)
    if err:
        log.warning("likes check failed: %s", err)

    def apply(d):  # re-read so edits made from the web page meanwhile are kept
        d.update(last_check=time.time(), last_error=err, access_token=snap["access_token"],
                 expires=snap["expires"], auth_error_notified=snap.get("auth_error_notified", 0))
        if liked is None or d.get("refresh_token") != snap["refresh_token"]:
            return
        if not d["initialized"]:
            # first run after connecting: remember what's already liked, don't spam
            d["seen"] = [v["id"] for v in liked][:MAX_SEEN]
            d["initialized"] = True
            log.info("likes initialized with %d existing videos", len(liked))
            return
        seen = set(d["seen"])
        new = [v for v in liked if v["id"] not in seen]
        for v in reversed(new):  # oldest first, so notifications arrive in like order
            item = dict(v, status="pending", found=time.time())
            item["notified"] = bool(notify.send("▶️ แตะเพื่อโพสต์ลง X", v["title"], tags=["arrow_forward"],
                                                click=intent_url(d, item)))
            d["items"].insert(0, item)
            d["seen"].insert(0, v["id"])
            log.info("new like: %s %s", v["id"], v["title"])
        d["items"] = d["items"][:MAX_ITEMS]
        d["seen"] = d["seen"][:MAX_SEEN]
    update(apply)


def mark(vid, status):
    def fn(d):
        for it in d["items"]:
            if it["id"] == vid:
                it["status"] = status
    update(fn)


def run_forever():
    time.sleep(20)
    while True:
        try:
            check()
        except Exception:
            log.exception("likes loop failed")
        time.sleep(30)
