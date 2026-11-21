"""Record one live: short segments, reconnect immediately, merge at the end."""
import json
import logging
import os
import shutil
import signal
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import notify, settings
from .tiktok import UA, TikTokError, stream_identity

log = logging.getLogger("record")

try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo("Asia/Bangkok")
except Exception:  # pragma: no cover
    TZ = timezone(timedelta(hours=7))

REFRESH_EVERY = int(os.environ.get("TKREC_REFRESH_EVERY", "45"))      # seconds: re-check stream URL while recording (catches mode switches)
STALL_SECONDS = 30      # no new bytes for this long -> reconnect
MIN_GOOD_BYTES = 200_000


def now_local():
    return datetime.now(TZ)


def fmt_duration(sec):
    sec = int(sec)
    h, m = divmod(sec // 60, 60)
    return f"{h} ชม. {m} นาที" if h else f"{m} นาที {sec % 60} วิ"


def fmt_size(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024


def write_info(folder, info):
    tmp = folder / "info.tmp"
    tmp.write_text(json.dumps(info, ensure_ascii=False, indent=2), "utf-8")
    tmp.replace(folder / "info.json")


def read_info(folder):
    try:
        return json.loads((folder / "info.json").read_text("utf-8"))
    except Exception:
        return None


class LiveSession(threading.Thread):
    def __init__(self, user, live, tk):
        super().__init__(name=f"rec-{user}", daemon=True)
        self.user = user
        self.live = live
        self.tk = tk
        self.stop_event = threading.Event()
        self.started = now_local()
        self.folder = settings.RECORDINGS_DIR / user / self.started.strftime("%Y-%m-%d_%H%M%S")
        self.folder.mkdir(parents=True, exist_ok=True)
        self.seg_no = 0
        self.proc = None
        self.info = {
            "user": user, "nickname": live.nickname, "title": live.title,
            "room_id": live.room_id, "started": self.started.isoformat(),
            "ended": None, "status": "recording", "files": [], "segments": 0,
        }
        write_info(self.folder, self.info)

    # ---------- main loop ----------
    def run(self):
        cfg = settings.load()
        log.info("@%s live (room %s) -> %s", self.user, self.live.room_id, self.folder)
        notify.send(f"🔴 @{self.user} ไลฟ์อยู่ — เริ่มอัดแล้ว",
                    (self.live.title or "ไม่มีชื่อไลฟ์") + f"\nเริ่ม {self.started:%H:%M} น.",
                    tags=["red_circle"], click=cfg.get("public_url") or None, priority=4)
        try:
            self._record_loop()
        except Exception:
            log.exception("@%s recording loop crashed", self.user)
        self.finish()

    def _record_loop(self):
        live = self.live
        dead_since = None
        while not self.stop_event.is_set():
            grace = settings.load().get("end_grace_seconds", 180)
            if live and live.streams:
                dead_since = None
                if not self._record_any(live):
                    time.sleep(3)
                live = None  # always fetch fresh URLs before the next segment
            else:
                if dead_since is None:
                    dead_since = time.time()
                    log.info("@%s stream gone, waiting up to %ss for it to come back", self.user, grace)
                if time.time() - dead_since > grace:
                    log.info("@%s live ended", self.user)
                    return
                self.stop_event.wait(5)
            if self.stop_event.is_set():
                return
            live = self._refresh()

    def _refresh(self):
        """Fresh stream URLs; also catches the live moving to a new room (e.g. mode switch)."""
        try:
            live = self.tk.refresh(self.live.room_id)
            if live and live.streams:
                return live
        except TikTokError as e:
            log.info("@%s refresh failed: %s", self.user, e)
        try:
            live = self.tk.get_live(self.user)
            if live and live.streams:
                if live.room_id != self.live.room_id:
                    log.info("@%s moved to new room %s", self.user, live.room_id)
                self.live = live
                self.info["room_id"] = live.room_id
                return live
        except TikTokError as e:
            log.info("@%s get_live failed: %s", self.user, e)
        return None

    def _record_any(self, live):
        """Try streams best-first until one actually delivers video."""
        for stream in live.streams[:4]:
            got = self._record_segment(stream)
            if got >= MIN_GOOD_BYTES or self.stop_event.is_set():
                return True
            log.info("@%s %s/%s gave %d bytes, trying next", self.user, stream["quality"], stream["kind"], got)
        return False

    def _record_segment(self, stream):
        self.seg_no += 1
        out = self.folder / f"seg_{self.seg_no:04d}.ts"
        cookies = settings.load().get("cookies") or ""
        headers = "Referer: https://www.tiktok.com/\r\n"
        if cookies:
            headers += f"Cookie: {cookies}\r\n"
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
               "-user_agent", UA, "-headers", headers,
               "-rw_timeout", "20000000"]
        if stream["kind"] == "hls":
            cmd += ["-live_start_index", "-3"]
        cmd += ["-i", stream["url"], "-map", "0:v?", "-map", "0:a?", "-c", "copy",
                "-f", "mpegts", str(out)]
        log.info("@%s segment %d: %s %s %sx%s", self.user, self.seg_no, stream["quality"],
                 stream["kind"], stream["width"], stream["height"])
        ident = stream_identity(stream["url"])
        self.proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        last_size, last_growth, last_refresh = 0, time.time(), time.time()
        reason = "ended"
        while self.proc.poll() is None:
            if self.stop_event.wait(1):
                reason = "stopped"
                break
            size = out.stat().st_size if out.exists() else 0
            if size > last_size:
                last_size, last_growth = size, time.time()
            elif time.time() - last_growth > STALL_SECONDS:
                reason = "stalled"
                break
            if time.time() - last_refresh > REFRESH_EVERY:
                last_refresh = time.time()
                if self._stream_changed(ident):
                    reason = "stream changed"
                    break
        self._stop_ffmpeg()
        err = (self.proc.stderr.read() or b"").decode("utf-8", "replace").strip()[-300:]
        size = out.stat().st_size if out.exists() else 0
        log.info("@%s segment %d done (%s, %s)%s", self.user, self.seg_no, reason, fmt_size(size),
                 f" ffmpeg: {err}" if err and size < MIN_GOOD_BYTES else "")
        if size < 20_000 and out.exists():
            out.unlink()
        self.info["segments"] = len(list(self.folder.glob("seg_*.ts")))
        write_info(self.folder, self.info)
        return size

    def _stream_changed(self, ident):
        try:
            live = self.tk.refresh(self.live.room_id)
        except TikTokError:
            return False
        if live is None:
            # room says ended; let ffmpeg run until data stops (it will stall/end on its own)
            return False
        if live.best and stream_identity(live.best["url"]) != ident:
            idents = {stream_identity(s["url"]) for s in live.streams}
            if ident not in idents:
                log.info("@%s stream id changed (%s -> %s)", self.user, ident, stream_identity(live.best["url"]))
                return True
        return False

    def _stop_ffmpeg(self):
        p = self.proc
        if p and p.poll() is None:
            p.send_signal(signal.SIGINT)
            try:
                p.wait(10)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait()

    def stop(self):
        self.stop_event.set()

    # ---------- finish ----------
    def finish(self):
        self.info["ended"] = now_local().isoformat()
        self.info["status"] = "merging"
        write_info(self.folder, self.info)
        files = finalize_folder(self.folder, self.info)
        cfg = settings.load()
        if files:
            total = sum(f["size"] for f in files)
            dur = sum(f.get("duration", 0) for f in files)
            parts = f" ({len(files)} ไฟล์)" if len(files) > 1 else ""
            notify.send(f"✅ อัดไลฟ์ @{self.user} เสร็จแล้ว",
                        f"ยาว {fmt_duration(dur)} · {fmt_size(total)}{parts}\nกดเพื่อดู/โหลด",
                        tags=["white_check_mark"], click=cfg.get("public_url") or None)
        else:
            notify.send(f"⚠️ @{self.user} ไลฟ์จบ แต่อัดไม่ได้",
                        "ดึงวิดีโอไม่ได้เลย — เปิดหน้าเว็บดูบันทึก (log) หรือลองใส่คุกกี้ TikTok",
                        tags=["warning"], click=cfg.get("public_url") or None, priority=4)


# ---------- merging (also used at startup for unfinished folders) ----------
def probe(path):
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries",
             "stream=codec_type,codec_name,width,height:format=duration", "-of", "json", str(path)],
            capture_output=True, timeout=60).stdout
        j = json.loads(out or b"{}")
    except Exception:
        return None
    v = next((s for s in j.get("streams", []) if s.get("codec_type") == "video"), {})
    a = next((s for s in j.get("streams", []) if s.get("codec_type") == "audio"), {})
    if not v and not a:
        return None
    return {"key": (v.get("codec_name"), v.get("width"), v.get("height"), a.get("codec_name")),
            "width": v.get("width") or 0, "height": v.get("height") or 0,
            "duration": float((j.get("format") or {}).get("duration") or 0)}


def finalize_folder(folder, info=None):
    folder = Path(folder)
    info = info or read_info(folder) or {}
    user = info.get("user") or folder.parent.name
    segs = sorted(folder.glob("seg_*.ts"))
    groups = []
    for seg in segs:
        p = probe(seg)
        if not p:
            continue
        if groups and groups[-1]["key"] == p["key"]:
            groups[-1]["segs"].append(seg)
            groups[-1]["duration"] += p["duration"]
        else:
            groups.append({"key": p["key"], "segs": [seg], "width": p["width"],
                           "height": p["height"], "duration": p["duration"]})

    keep_ts = settings.load().get("keep_ts")
    files, ok_all = [], True
    base = f"{user}_{folder.name[:15]}"
    for i, g in enumerate(groups, 1):
        name = base + (f"_part{i}" if len(groups) > 1 else "")
        mp4 = folder / f"{name}.mp4"
        ts = folder / f"{name}.ts"
        ok = _concat_mp4(folder, g["segs"], mp4)
        if not ok or keep_ts:
            with open(ts, "wb") as dst:
                for s in g["segs"]:
                    with open(s, "rb") as src:
                        shutil.copyfileobj(src, dst, 4 << 20)
        for path in ([mp4] if ok else []) + ([ts] if ts.exists() else []):
            files.append({"name": path.name, "size": path.stat().st_size,
                          "duration": round(g["duration"]), "width": g["width"], "height": g["height"]})
        ok_all = ok_all and (ok or ts.exists())

    if ok_all:
        for s in segs:
            s.unlink(missing_ok=True)
    (folder / "concat.txt").unlink(missing_ok=True)
    info["files"] = files
    info["segments"] = 0 if ok_all else len(segs)
    info["status"] = "done" if files else "failed"
    if not info.get("ended"):
        info["ended"] = now_local().isoformat()
    write_info(folder, info)
    return files


def _concat_mp4(folder, segs, out):
    lst = folder / "concat.txt"
    lst.write_text("".join(f"file '{s.name}'\n" for s in segs), "utf-8")
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
           "-f", "concat", "-safe", "0", "-i", str(lst),
           "-map", "0:v?", "-map", "0:a?", "-c", "copy", "-movflags", "+faststart", str(out)]
    r = subprocess.run(cmd, capture_output=True)
    if r.returncode != 0 or not out.exists() or out.stat().st_size < 10_000:
        log.warning("mp4 merge failed in %s: %s", folder, r.stderr.decode("utf-8", "replace")[-300:])
        out.unlink(missing_ok=True)
        return False
    return True


def finalize_orphans():
    """After a crash/reboot, merge folders that were still 'recording'."""
    if not settings.RECORDINGS_DIR.exists():
        return
    for info_file in settings.RECORDINGS_DIR.glob("*/*/info.json"):
        info = read_info(info_file.parent)
        if info and info.get("status") in ("recording", "merging"):
            log.info("finishing unfinished recording %s", info_file.parent)
            info["ended"] = info.get("ended") or datetime.fromtimestamp(
                info_file.stat().st_mtime, TZ).isoformat()
            finalize_folder(info_file.parent, info)


def cleanup():
    """Delete old recordings (retention days) and oldest ones when the disk is nearly full."""
    cfg = settings.load()
    root = settings.RECORDINGS_DIR
    if not root.exists():
        return
    folders = []
    for info_file in root.glob("*/*/info.json"):
        info = read_info(info_file.parent) or {}
        if info.get("status") == "done" and not info.get("pinned"):
            folders.append((info_file.stat().st_mtime, info_file.parent))
    folders.sort()
    days = int(cfg.get("retention_days") or 0)
    if days > 0:
        cutoff = time.time() - days * 86400
        for mtime, f in list(folders):
            if mtime < cutoff:
                log.info("retention: deleting %s", f)
                shutil.rmtree(f, ignore_errors=True)
                folders.remove((mtime, f))
    min_free = float(cfg.get("min_free_gb") or 0) * (1 << 30)
    while folders and shutil.disk_usage(root).free < min_free:
        mtime, f = folders.pop(0)
        log.info("disk nearly full: deleting oldest %s", f)
        shutil.rmtree(f, ignore_errors=True)
