"""Talking to Instagram: is a user live, and where is the video (DASH).

Instagram only shows lives to logged-in users, so a cookie string from an
Instagram account (best: a spare account) is required.
Several endpoints are tried so that one change on Instagram's side doesn't break everything.
"""
import gzip
import json
import logging
import os
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


class Instagram:
    def __init__(self, cookies=""):
        self.cookies = (cookies or "").strip()
        self._ids = {}         # username -> user id
        self._by_room = {}     # broadcast id -> username

    def _csrf(self):
        for part in self.cookies.split(";"):
            k, _, v = part.strip().partition("=")
            if k == "csrftoken":
                return v
        return ""

    def _get_json(self, url, mobile=False):
        if not self.cookies:
            raise NeedLogin("ยังไม่ได้ใส่คุกกี้ Instagram")
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
                raise Blocked("Instagram จำกัดการเรียกถี่เกินไป (HTTP 429)")
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
        return j

    def user_id(self, user):
        if user in self._ids:
            return self._ids[user]
        j = self._get_json(f"{WWW}/api/v1/users/web_profile_info/?username={urllib.parse.quote(user)}")
        uid = (((j.get("data") or {}).get("user")) or {}).get("id")
        if not uid:
            raise LiveError(f"ไม่เจอบัญชี IG @{user}")
        self._ids[user] = str(uid)
        return self._ids[user]

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
