"""Talking to TikTok: find out if a user is live and get the best stream URL.

Several methods are tried in order so that if TikTok changes or blocks one,
the others still work.
"""
import gzip
import json
import logging
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib

log = logging.getLogger("tiktok")

WWW = os.environ.get("TKREC_TIKTOK_WWW", "https://www.tiktok.com")
WEBCAST = os.environ.get("TKREC_TIKTOK_WEBCAST", "https://webcast.tiktok.com")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

# TikTok's quality names, best first (used when resolution info is missing)
QUALITY_ORDER = ["origin", "uhd_60", "uhd", "hd_60", "hd", "sd", "ld", "ao"]
FLV_ORDER = ["FULL_HD1", "HD1", "SD1", "SD2"]


class TikTokError(Exception):
    pass


class Blocked(TikTokError):
    """TikTok refused us (captcha / login wall / region)."""


class LiveInfo:
    def __init__(self, room_id, title="", nickname="", streams=None):
        self.room_id = room_id
        self.title = title
        self.nickname = nickname
        self.streams = streams or []   # list of dicts: url, kind, quality, width, height, bitrate

    @property
    def best(self):
        return self.streams[0] if self.streams else None

    def __repr__(self):
        return f"LiveInfo(room={self.room_id}, streams={len(self.streams)})"


class TikTok:
    def __init__(self, cookies=""):
        self.cookies = cookies or ""
        self._room_cache = {}  # user -> room_id

    # ---------- HTTP ----------
    def _get(self, url, params=None, timeout=15):
        if params:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
        headers = {
            "User-Agent": UA,
            "Accept": "text/html,application/json,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9,th;q=0.8",
            "Accept-Encoding": "gzip, deflate",
            "Referer": WWW + "/",
        }
        if self.cookies:
            headers["Cookie"] = self.cookies
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                body = r.read()
                enc = (r.headers.get("Content-Encoding") or "").lower()
        except urllib.error.HTTPError as e:
            if e.code in (403, 429):
                raise Blocked(f"HTTP {e.code} from {url.split('?')[0]}")
            raise TikTokError(f"HTTP {e.code} from {url.split('?')[0]}")
        except Exception as e:
            raise TikTokError(f"{type(e).__name__}: {e}")
        if enc == "gzip":
            body = gzip.decompress(body)
        elif enc == "deflate":
            body = zlib.decompress(body)
        return body.decode("utf-8", "replace")

    def _get_json(self, url, params=None):
        text = self._get(url, params)
        try:
            return json.loads(text)
        except ValueError:
            if "captcha" in text.lower() or "verify" in text.lower():
                raise Blocked("TikTok asked for captcha")
            raise TikTokError("not JSON: " + text[:120])

    # ---------- room id ----------
    def _room_id_api(self, user):
        j = self._get_json(WWW + "/api-live/user/room/", {
            "aid": "1988", "app_language": "en", "app_name": "tiktok_web",
            "browser_language": "en-US", "browser_name": "Mozilla", "browser_platform": "Win32",
            "device_platform": "web_pc", "region": "SG", "sourceType": "54", "uniqueId": user,
        })
        data = j.get("data") or {}
        room_id = str((data.get("user") or {}).get("roomId") or "")
        status = (data.get("liveRoom") or {}).get("status")
        if j.get("statusCode") not in (0, None) and not room_id:
            raise TikTokError(f"api-live statusCode={j.get('statusCode')} {j.get('message','')}")
        return room_id or None, status

    def _room_id_html(self, user):
        html = self._get(f"{WWW}/@{urllib.parse.quote(user)}/live")
        for pat in (r'"roomId":"(\d+)"', r'room_id=(\d+)', r'"room_id":"?(\d+)'):
            m = re.search(pat, html)
            if m and m.group(1) != "0":
                return m.group(1)
        if "captcha" in html.lower()[:5000]:
            raise Blocked("captcha on live page")
        return None

    def get_room_id(self, user):
        errors = []
        try:
            room_id, _ = self._room_id_api(user)
            if room_id:
                self._room_cache[user] = room_id
                return room_id
        except TikTokError as e:
            errors.append(f"api: {e}")
        try:
            room_id = self._room_id_html(user)
            if room_id:
                self._room_cache[user] = room_id
                return room_id
        except TikTokError as e:
            errors.append(f"html: {e}")
        if errors and len(errors) == 2:
            # both failed with errors -> fall back to the last known room (lives often reuse it)
            if user in self._room_cache:
                return self._room_cache[user]
            if any("HTTP 4" in x or "captcha" in x for x in errors):
                raise Blocked("; ".join(errors))
            raise TikTokError("; ".join(errors))
        return None

    # ---------- room state ----------
    def is_alive(self, room_id):
        try:
            j = self._get_json(WEBCAST + "/webcast/room/check_alive/", {
                "aid": "1988", "region": "CH", "room_ids": room_id, "user_is_login": "true"})
            items = j.get("data") or []
            if items:
                return bool(items[0].get("alive"))
        except TikTokError as e:
            log.debug("check_alive failed: %s", e)
        info = self.room_info(room_id)
        return info.get("status") == 2

    def room_info(self, room_id):
        j = self._get_json(WEBCAST + "/webcast/room/info/", {"aid": "1988", "room_id": room_id})
        return j.get("data") or {}

    def get_live(self, user):
        """Return LiveInfo if user is live right now, otherwise None."""
        room_id = self.get_room_id(user)
        if not room_id:
            return None
        data = self.room_info(room_id)
        if data.get("status") != 2:
            return None
        streams = extract_streams(data)
        owner = data.get("owner") or {}
        return LiveInfo(room_id, data.get("title", ""), owner.get("nickname", ""), streams)

    def refresh(self, room_id):
        """Re-read stream URLs of an ongoing room. None if no longer live."""
        data = self.room_info(room_id)
        if data.get("status") != 2:
            return None
        owner = data.get("owner") or {}
        return LiveInfo(room_id, data.get("title", ""), owner.get("nickname", ""), extract_streams(data))


def _res(s):
    m = re.match(r"(\d+)\s*[x*]\s*(\d+)", str(s or ""))
    return (int(m.group(1)), int(m.group(2))) if m else (0, 0)


def extract_streams(data):
    """All stream URLs in the room, best quality first."""
    su = data.get("stream_url") or {}
    found = []

    # 1) live_core_sdk_data: has every quality incl. "origin" (source) + resolution info
    for sdk in _iter_sdk_data(data):
        for q, item in sdk.items():
            main = (item or {}).get("main") or {}
            params = main.get("sdk_params") or "{}"
            try:
                params = json.loads(params) if isinstance(params, str) else params
            except ValueError:
                params = {}
            w, h = _res(params.get("resolution"))
            br = int(params.get("vbitrate") or 0)
            for kind in ("flv", "hls"):
                if main.get(kind):
                    found.append({"url": main[kind], "kind": kind, "quality": q,
                                  "width": w, "height": h, "bitrate": br})

    # 2) classic flv_pull_url dict
    flv = su.get("flv_pull_url") or {}
    for i, key in enumerate(FLV_ORDER):
        if flv.get(key):
            found.append({"url": flv[key], "kind": "flv", "quality": key,
                          "width": 0, "height": 0, "bitrate": 0, "_rank": i})
    for key, url in flv.items():
        if key not in FLV_ORDER and url:
            found.append({"url": url, "kind": "flv", "quality": key, "width": 0, "height": 0, "bitrate": 0})

    # 3) single URLs
    if su.get("hls_pull_url"):
        found.append({"url": su["hls_pull_url"], "kind": "hls", "quality": "hls", "width": 0, "height": 0, "bitrate": 0})
    if su.get("rtmp_pull_url"):
        found.append({"url": su["rtmp_pull_url"], "kind": "flv", "quality": "rtmp", "width": 0, "height": 0, "bitrate": 0})

    def score(s):
        q = s["quality"]
        qrank = QUALITY_ORDER.index(q) if q in QUALITY_ORDER else (
            10 + s.get("_rank", 5))
        if q == "ao":
            qrank = 99  # audio only = last resort
        # "origin" = the streamer's own upload, always the best available, even when
        # TikTok leaves out its resolution info (it often does)
        origin = q == "origin" or "_or4" in urllib.parse.urlparse(s["url"]).path
        return (0 if origin else 1, -(s["width"] * s["height"]), -s["bitrate"], qrank,
                0 if s["kind"] == "flv" else 1)

    found.sort(key=score)

    seen, out = set(), []
    for s in found:
        if s["url"] not in seen:
            seen.add(s["url"])
            s.pop("_rank", None)
            out.append(s)
    return out


def _iter_sdk_data(data):
    su = data.get("stream_url") or {}
    blobs = [((su.get("live_core_sdk_data") or {}).get("pull_data") or {}).get("stream_data")]
    # multi-stream / game rooms sometimes put extra pull data here
    for extra in (data.get("multi_stream_data") or {}).get("pull_datas", {}).values() if isinstance(
            (data.get("multi_stream_data") or {}).get("pull_datas"), dict) else []:
        blobs.append((extra or {}).get("stream_data"))
    for blob in blobs:
        if not blob:
            continue
        try:
            j = json.loads(blob) if isinstance(blob, str) else blob
        except ValueError:
            continue
        d = j.get("data") or {}
        if isinstance(d, dict) and d:
            yield d


def stream_identity(url):
    """Part of the URL that identifies the stream (ignores expiring signature params)."""
    p = urllib.parse.urlparse(url)
    return p.path.rsplit("/", 1)[-1].split(".")[0].split("_")[0]
