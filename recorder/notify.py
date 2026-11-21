"""Push notifications through ntfy (https://ntfy.sh)."""
import json
import logging
import urllib.request

from . import settings

log = logging.getLogger("notify")


def send(title, message, tags=None, click=None, priority=3):
    cfg = settings.load()
    topic = cfg.get("ntfy_topic")
    if not topic:
        return False
    body = {"topic": topic, "title": title, "message": message, "priority": priority}
    if tags:
        body["tags"] = tags
    if click:
        body["click"] = click
    req = urllib.request.Request(
        cfg.get("ntfy_server", "https://ntfy.sh").rstrip("/") + "/",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        urllib.request.urlopen(req, timeout=15).read()
        return True
    except Exception as e:
        log.warning("ntfy failed: %s", e)
        return False
