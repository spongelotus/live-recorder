"""Settings stored as JSON in the data folder (editable from the web page)."""
import json
import os
import threading
from pathlib import Path

DATA_DIR = Path(os.environ.get("TKREC_DATA", "/var/lib/live-recorder"))
SETTINGS_FILE = DATA_DIR / "settings.json"
RECORDINGS_DIR = DATA_DIR / "recordings"
LOG_FILE = DATA_DIR / "recorder.log"
RESTART_FLAG = DATA_DIR / "restart-pending"

DEFAULTS = {
    "accounts": [],            # TikTok usernames without @
    "check_interval": 60,      # seconds between checks of each account
    "ntfy_server": "https://ntfy.sh",
    "ntfy_topic": "",
    "web_password": "",
    "secret": "",
    "public_url": "",
    "retention_days": 0,       # 0 = keep forever
    "min_free_gb": 10,         # delete oldest recordings when free disk drops below this
    "cookies": "",             # optional TikTok cookie string (sessionid=...; ...)
    "end_grace_seconds": 180,  # wait this long after stream drops before calling the live finished
    "keep_ts": False,          # also keep the merged .ts besides .mp4
}

_lock = threading.Lock()


def load():
    with _lock:
        data = dict(DEFAULTS)
        if SETTINGS_FILE.exists():
            try:
                data.update(json.loads(SETTINGS_FILE.read_text("utf-8")))
            except Exception:
                pass
        data["accounts"] = [clean_username(a) for a in data.get("accounts", []) if clean_username(a)]
        data["check_interval"] = max(20, int(data.get("check_interval") or 60))
        return data


def save(data):
    with _lock:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = SETTINGS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")
        os.chmod(tmp, 0o600)
        tmp.replace(SETTINGS_FILE)


def update(**changes):
    data = load()
    data.update(changes)
    save(data)
    return data


def clean_username(name):
    name = (name or "").strip()
    if "tiktok.com/" in name:
        # accept a pasted profile/live link
        part = name.split("tiktok.com/", 1)[1]
        name = part.split("/")[0].split("?")[0]
    name = name.lstrip("@").strip()
    return "".join(ch for ch in name if ch.isalnum() or ch in "._")
