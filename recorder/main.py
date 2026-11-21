"""Entry point: watch accounts, start recordings, run the web page."""
import logging
import logging.handlers
import os
import sys
import threading
import time

from . import notify, record, settings, web
from .tiktok import Blocked, TikTok, TikTokError

log = logging.getLogger("main")


class Manager:
    def __init__(self):
        self.sessions = {}          # user -> LiveSession
        self.status = {}            # user -> dict(state, checked, error)
        self.last_check = {}
        self.blocked_notified = 0
        self.lock = threading.Lock()
        self.stopping = False

    def active(self):
        with self.lock:
            return {u: s for u, s in self.sessions.items() if s.is_alive()}

    def tick(self):
        cfg = settings.load()
        tk = TikTok(cfg.get("cookies"))
        accounts = cfg["accounts"]
        # stop recording accounts that were removed
        for user, sess in list(self.active().items()):
            if user not in accounts:
                log.info("@%s removed from list, stopping recording", user)
                sess.stop()
        for user in accounts:
            if self.stopping:
                return
            if user in self.active():
                self.status[user] = {"state": "recording", "checked": time.time(),
                                     "folder": self.sessions[user].folder.name}
                continue
            if time.time() - self.last_check.get(user, 0) < cfg["check_interval"]:
                continue
            self.last_check[user] = time.time()
            try:
                live = tk.get_live(user)
            except Blocked as e:
                self.status[user] = {"state": "error", "checked": time.time(), "error": str(e)}
                log.warning("@%s blocked: %s", user, e)
                if time.time() - self.blocked_notified > 6 * 3600:
                    self.blocked_notified = time.time()
                    notify.send("⚠️ TikTok บล็อกการเช็กไลฟ์",
                                f"เช็ก @{user} ไม่ได้ ({e}) — ลองใส่คุกกี้ TikTok ในหน้าตั้งค่า",
                                tags=["warning"], click=cfg.get("public_url") or None, priority=4)
                continue
            except TikTokError as e:
                self.status[user] = {"state": "error", "checked": time.time(), "error": str(e)}
                log.info("@%s check failed: %s", user, e)
                continue
            if live and live.streams:
                sess = record.LiveSession(user, live, tk)
                with self.lock:
                    self.sessions[user] = sess
                sess.start()
                self.status[user] = {"state": "recording", "checked": time.time(), "folder": sess.folder.name}
            elif live:
                self.status[user] = {"state": "error", "checked": time.time(),
                                     "error": "ไลฟ์อยู่แต่ไม่เจอลิงก์วิดีโอ (อาจต้องใช้คุกกี้)"}
            else:
                self.status[user] = {"state": "offline", "checked": time.time()}
            time.sleep(1.5)  # be gentle with TikTok
        for user in list(self.status):
            if user not in accounts:
                self.status.pop(user, None)

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
    log.info("started; watching %s", settings.load()["accounts"])
    mgr.run()


if __name__ == "__main__":
    main()
