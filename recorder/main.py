"""Entry point: watch accounts, start recordings, run the web page."""
import logging
import logging.handlers
import os
import sys
import threading
import time
from datetime import datetime

from . import notify, record, settings, web
from .instagram import Instagram
from .tiktok import Blocked, TikTok, TikTokError

log = logging.getLogger("main")


class Manager:
    def __init__(self):
        self.sessions = {}          # user -> LiveSession
        self.status = {}            # user -> dict(state, checked, error)
        self.last_check = {}
        self.blocked_notified = {}
        self.lock = threading.Lock()
        self.stopping = False

    def active(self):
        with self.lock:
            return {u: s for u, s in self.sessions.items() if s.is_alive()}

    def _client(self, platform, cookies):
        key = (platform, cookies)
        if getattr(self, "_clients", {}).get(platform, (None, None))[0] != key:
            self._clients = getattr(self, "_clients", {})
            self._clients[platform] = (key, Instagram(cookies) if platform == "instagram" else TikTok(cookies))
        return self._clients[platform][1]

    def tick(self):
        cfg = settings.load()
        targets = [("tiktok", u, u) for u in cfg["accounts"]]
        targets += [("instagram", u, "ig:" + u) for u in cfg["ig_accounts"]]
        keys = {k for _, _, k in targets}
        # stop recording accounts that were removed
        for key, sess in list(self.active().items()):
            if key not in keys:
                log.info("%s removed from list, stopping recording", key)
                sess.stop()
        for platform, user, key in targets:
            if self.stopping:
                return
            if key in self.active():
                self.status[key] = {"state": "recording", "checked": time.time(),
                                    "folder": self.sessions[key].folder.name}
                continue
            interval = cfg["ig_check_interval"] if platform == "instagram" else cfg["check_interval"]
            if time.time() - self.last_check.get(key, 0) < interval:
                continue
            self.last_check[key] = time.time()
            client = self._client(platform, cfg.get("ig_cookies") if platform == "instagram" else cfg.get("cookies"))
            name = "Instagram" if platform == "instagram" else "TikTok"
            try:
                live = client.get_live(user)
            except Blocked as e:
                self.status[key] = {"state": "error", "checked": time.time(), "error": str(e)}
                log.warning("%s blocked: %s", key, e)
                if time.time() - self.blocked_notified.get(platform, 0) > 6 * 3600:
                    self.blocked_notified[platform] = time.time()
                    notify.send(f"⚠️ {name} เช็กไลฟ์ไม่ได้",
                                f"เช็ก @{user} ไม่ได้ ({e}) — ไปที่หน้าตั้งค่าแล้วใส่/อัปเดตคุกกี้ {name}",
                                tags=["warning"], click=cfg.get("public_url") or None, priority=4)
                continue
            except TikTokError as e:
                self.status[key] = {"state": "error", "checked": time.time(), "error": str(e)}
                log.info("%s check failed: %s", key, e)
                continue
            if live and live.streams:
                sess = record.LiveSession(user, live, client, platform)
                with self.lock:
                    self.sessions[key] = sess
                sess.start()
                self.status[key] = {"state": "recording", "checked": time.time(), "folder": sess.folder.name}
            elif live:
                self.status[key] = {"state": "error", "checked": time.time(),
                                    "error": "ไลฟ์อยู่แต่ไม่เจอลิงก์วิดีโอ (อาจต้องใช้คุกกี้)"}
            else:
                self.status[key] = {"state": "offline", "checked": time.time()}
            time.sleep(1.5)  # be gentle
        for key in list(self.status):
            if key not in keys:
                self.status.pop(key, None)

    def run(self):
        record.finalize_orphans()
        last_cleanup = 0
        while not self.stopping:
            try:
                self.tick()
            except Exception:
                log.exception("tick failed")
            if time.time() - last_cleanup > 3600:
                last_cleanup = time.time()
                try:
                    record.cleanup()
                except Exception:
                    log.exception("cleanup failed")
            # self-update: restart only when nothing is recording
            if settings.RESTART_FLAG.exists() and not self.active():
                settings.RESTART_FLAG.unlink(missing_ok=True)
                log.info("update installed, restarting")
                os._exit(3)  # systemd restarts us
            time.sleep(5)

    def shutdown(self, *_):
        self.stopping = True
        for sess in self.active().values():
            sess.stop()
        for sess in list(self.sessions.values()):
            sess.join(timeout=600)  # allow merge to finish
        sys.exit(0)


def setup_logging():
    settings.DATA_DIR.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S")
    fmt.converter = lambda ts: datetime.fromtimestamp(ts, record.TZ).timetuple()  # Thai time
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fh = logging.handlers.RotatingFileHandler(settings.LOG_FILE, maxBytes=2_000_000, backupCount=3,
                                              encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    root.addHandler(sh)


def main():
    import signal
    setup_logging()
    mgr = Manager()
    signal.signal(signal.SIGTERM, mgr.shutdown)
    signal.signal(signal.SIGINT, mgr.shutdown)
    port = int(os.environ.get("TKREC_PORT", "8080"))
    host = os.environ.get("TKREC_HOST", "127.0.0.1")
    threading.Thread(target=web.serve, args=(mgr, host, port), daemon=True, name="web").start()
    log.info("started; TikTok %s, IG %s", settings.load()["accounts"], settings.load()["ig_accounts"])
    mgr.run()


if __name__ == "__main__":
    main()
