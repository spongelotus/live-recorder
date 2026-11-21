"""Talking to Instagram: is a user live, and where is the video (DASH).

Instagram only shows lives to logged-in users, so a cookie string from an
Instagram account (best: a spare account) is required.

To stay under Instagram's rate limits (cloud servers get HTTP 429 quickly):
- ONE request covers every watched account: the "reels tray" of the cookie account
  lists all lives of accounts it follows → the spare account must FOLLOW the targets.
- The tray answer is shared by all accounts for a short while.
- On HTTP 429 every Instagram request pauses (5 → 10 → 20 → 40 → 60 min).
- Per-user endpoints (incl. the strict profile lookup) are only a fallback if the
  tray endpoint stops working, and user ids are cached on disk.
"""
import gzip
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib

from .tiktok import Blocked, LiveInfo, TikTokError as LiveError

log = logging.getLogger("instagram")

WWW = os.environ.get("TKREC_IG_WWW", "https://www.instagram.com")
API = os.environ.get("TKREC_IG_API", "https://i.instagram.com")

WEB_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
WEB_APP_ID = "936619743392459"
MOBILE_UA = ("Instagram 361.0.0.46.88 Android (33/13; 420dpi; 1080x2340; samsung; SM-S911B; "
             "dm1q; qcom; en_US; 674675155)")
MOBILE_APP_ID = "567067343352427"


class NeedLogin(Blocked):
    pass


_pause = {"until": 0.0, "step": 0}
_pause_lock = threading.Lock()
TRAY_TTL = 40          # seconds the tray answer is reused for all accounts


def _ids_file():
    from . import settings
    return settings.DATA_DIR / "ig_ids.json"


def paused_until():
    return _pause["until"]


class Instagram:
    def __init__(self, cookies=""):
        self.cookies = (cookies or "").strip()
        self._by_room = {}     # broadcast id -> username
        self._tray = None      # (time, {username: broadcast}) or None
        self._tray_broken = False
        self._tray_lock = threading.Lock()
        try:
            self._ids = json.loads(_ids_file().read_text("utf-8"))
        except Exception:
            self._ids = {}     # username -> user id (persisted)

    def _csrf(self):
        for part in self.cookies.split(";"):
            k, _, v = part.strip().partition("=")
            if k == "csrftoken":
                return v
        return ""

    def _get_json(self, url, mobile=False):
        if not self.cookies:
            raise NeedLogin("ยังไม่ได้ใส่คุกกี้ Instagram")
        if time.time() < _pause["until"]:
            left = int((_pause["until"] - time.time()) / 60) + 1
            raise Blocked(f"Instagram จำกัดการเรียก (429) — พักเช็กอีก ~{left} นาที แล้วจะลองใหม่เอง")
        headers = {
            "User-Agent": MOBILE_UA if mobile else WEB_UA,
            "X-IG-App-ID": MOBILE_APP_ID if mobile else WEB_APP_ID,
            "Accept": "application/json",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate",
            "Cookie": self.cookies,
            "Referer": WWW + "/",
            "X-Requested-With": "XMLHttpRequest",
        }
        if self._csrf():
            headers["X-CSRFToken"] = self._csrf()
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                body, enc = r.read(), (r.headers.get("Content-Encoding") or "").lower()
        except urllib.error.HTTPError as e:
            text = e.read()[:300].decode("utf-8", "replace")
            if e.code in (401, 403) or "login_required" in text or "checkpoint" in text:
                raise NeedLogin(f"Instagram ให้ล็อกอินใหม่ (HTTP {e.code}) — คุกกี้หมดอายุหรือบัญชีโดนให้ยืนยันตัวตน")
            if e.code == 429:
                with _pause_lock:
                    mins = min(60, 5 * (2 ** _pause["step"]))
                    _pause["step"] = min(_pause["step"] + 1, 4)
                    _pause["until"] = time.time() + mins * 60
                log.warning("instagram 429 on %s — pausing %d min", url.split("?")[0], mins)
                raise Blocked(f"Instagram จำกัดการเรียก (429) — พักเช็ก {mins} นาที แล้วจะลองใหม่เอง")
            if e.code in (400, 404):
                return {"_status": e.code, "_text": text}
            raise LiveError(f"HTTP {e.code} from {url.split('?')[0]}")
        except Exception as e:
            raise LiveError(f"{type(e).__name__}: {e}")
        if enc == "gzip":
            body = gzip.decompress(body)
        elif enc == "deflate":
            body = zlib.decompress(body)
        text = body.decode("utf-8", "replace")
        try:
            j = json.loads(text)
        except ValueError:
            if "login" in text[:3000].lower():
                raise NeedLogin("Instagram ส่งหน้าล็อกอินกลับมา — คุกกี้หมดอายุ")
            raise LiveError("Instagram ตอบไม่ใช่ JSON")
        if j.get("message") in ("login_required", "checkpoint_required") or j.get("require_login"):
            raise NeedLogin(f"Instagram: {j.get('message')}")
        if _pause["step"]:
            _pause["step"] = max(0, _pause["step"] - 1)  # calm again → shorter pause next time
        return j

    def user_id(self, user):
        if user in self._ids:
            return self._ids[user]
        j = self._get_json(f"{WWW}/api/v1/users/web_profile_info/?username={urllib.parse.quote(user)}")
        uid = (((j.get("data") or {}).get("user")) or {}).get("id")
        if not uid:
            raise LiveError(f"ไม่เจอบัญชี IG @{user}")
        self._ids[user] = str(uid)
        try:
            _ids_file().write_text(json.dumps(self._ids), "utf-8")
        except Exception:
            pass
        return self._ids[user]

    @staticmethod
    def _has_stream(b):
        return bool(b and (b.get("dash_playback_url") or b.get("dash_abr_playback_url")))

    def tray(self):
        """{username: broadcast} for every live among accounts the cookie account follows (1 request)."""
        with self._tray_lock:
            if self._tray and time.time() - self._tray[0] < TRAY_TTL:
                return self._tray[1]
            j = self._get_json(f"{API}/api/v1/feed/reels_tray/", mobile=True)
            if j.get("_status"):
                raise LiveError(f"reels_tray HTTP {j['_status']}")
            out = {}
            items = list(j.get("broadcasts") or [])
            for r in j.get("tray") or []:
                if r.get("broadcast"):
                    items.append(r["broadcast"])
            for b in items:
                owner = (b.get("broadcast_owner") or {}).get("username", "").lower()
                if owner and self._has_stream(b):
                    out[owner] = b
            self._tray = (time.time(), out)
            return out

    def _broadcast(self, user):
        uid = self.user_id(user)
        # 1) web endpoint
        j = self._get_json(f"{WWW}/api/v1/live/web_info/?target_user_id={uid}")
        if j.get("dash_playback_url") or j.get("dash_abr_playback_url"):
            return j
        # 2) mobile story feed (has "broadcast" while live)
        j2 = self._get_json(f"{API}/api/v1/feed/user/{uid}/story/", mobile=True)
        b = j2.get("broadcast") or ((j2.get("reel") or {}).get("broadcast"))
        if b and (b.get("dash_playback_url") or b.get("dash_abr_playback_url")):
            return b
        return None

    def get_live(self, user):
        b = None
        if not self._tray_broken:
            try:
                b = self.tray().get(user.lower())
            except Blocked:
                raise
            except LiveError as e:
                # endpoint changed/unavailable → fall back to per-user checks (more requests)
                log.warning("instagram tray unavailable (%s) — using per-user checks", e)
                self._tray_broken = True
        if self._tray_broken:
            b = self._broadcast(user)
        if not b or b.get("broadcast_status") in ("stopped", "post_live", "interrupted_ended"):
            return None
        room = str(b.get("id") or b.get("broadcast_id") or user)
        self._by_room[room] = user
        streams = []
        for key in ("dash_abr_playback_url", "dash_playback_url", "dash_live_predictive_playback_url"):
            if b.get(key):
                streams.append({"url": b[key], "kind": "dash", "quality": key.replace("_playback_url", ""),
                                "width": 0, "height": 0, "bitrate": 0})
        owner = b.get("broadcast_owner") or {}
        return LiveInfo(room, b.get("broadcast_message") or "", owner.get("full_name") or "", streams)

    def refresh(self, room_id):
        user = self._by_room.get(room_id)
        if not user:
            return None
        live = self.get_live(user)
        return live if live and live.room_id == room_id else None
