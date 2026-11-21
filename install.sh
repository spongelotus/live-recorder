#!/usr/bin/env bash
# Live Recorder — one-command installer for Ubuntu (Oracle Cloud ARM or any VPS)
#   curl -fsSL https://raw.githubusercontent.com/spongelotus/live-recorder/main/install.sh | sudo bash
set -euo pipefail

REPO="${TKREC_REPO:-https://github.com/spongelotus/live-recorder.git}"
BRANCH="${TKREC_BRANCH:-main}"
APP=/opt/live-recorder
DATA=/var/lib/live-recorder

if [ "$(id -u)" != 0 ]; then echo "กรุณารันด้วย sudo"; exit 1; fi
export DEBIAN_FRONTEND=noninteractive

step() { echo; echo "==> $*"; }

step "[1/7] ติดตั้งโปรแกรมที่ต้องใช้ (ffmpeg, python, git) — รอสักครู่"
echo iptables-persistent iptables-persistent/autosave_v4 boolean true | debconf-set-selections
echo iptables-persistent iptables-persistent/autosave_v6 boolean true | debconf-set-selections
apt-get update -qq
apt-get install -y -qq ffmpeg python3 tzdata git curl ca-certificates gnupg \
  debian-keyring debian-archive-keyring apt-transport-https iptables-persistent >/dev/null

step "[2/7] ติดตั้ง Caddy (ทำหน้าเว็บเป็น https อัตโนมัติ)"
if ! command -v caddy >/dev/null; then
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
    | gpg --batch --yes --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
    > /etc/apt/sources.list.d/caddy-stable.list
  apt-get update -qq
  apt-get install -y -qq caddy >/dev/null
fi

step "[3/7] ดาวน์โหลดโค้ด"
if [ -d "$APP/.git" ]; then
  git -C "$APP" fetch -q origin "$BRANCH"
  git -C "$APP" reset -q --hard "origin/$BRANCH"
else
  rm -rf "$APP"
  git clone -q -b "$BRANCH" "$REPO" "$APP"
fi

step "[4/7] สร้างผู้ใช้ระบบและโฟลเดอร์เก็บไลฟ์"
id liverec >/dev/null 2>&1 || useradd --system --home "$DATA" --shell /usr/sbin/nologin liverec
mkdir -p "$DATA/recordings"

PUBLIC_IP="$(curl -fsS --max-time 10 https://api.ipify.org || curl -fsS --max-time 10 https://ifconfig.me || true)"
DOMAIN="${TKREC_DOMAIN:-${PUBLIC_IP//./-}.sslip.io}"

TKREC_DATA="$DATA" TKREC_URL="https://$DOMAIN" python3 - <<'PY'
import json, os, secrets, string
path = os.path.join(os.environ["TKREC_DATA"], "settings.json")
cfg = {}
if os.path.exists(path):
    cfg = json.load(open(path, encoding="utf-8"))
alphabet = "abcdefghjkmnpqrstuvwxyz23456789"
cfg.setdefault("accounts", [])
cfg.setdefault("check_interval", 60)
cfg.setdefault("ntfy_server", "https://ntfy.sh")
if not cfg.get("ntfy_topic"):
    cfg["ntfy_topic"] = "bua-live-" + "".join(secrets.choice(alphabet) for _ in range(24))
if not cfg.get("web_password"):
    cfg["web_password"] = "-".join("".join(secrets.choice(alphabet) for _ in range(4)) for _ in range(3))
if not cfg.get("secret"):
    cfg["secret"] = secrets.token_hex(32)
cfg["public_url"] = os.environ["TKREC_URL"]
cfg.setdefault("retention_days", 0)
cfg.setdefault("min_free_gb", 10)
cfg.setdefault("end_grace_seconds", 180)
with open(path, "w", encoding="utf-8") as f:
    json.dump(cfg, f, ensure_ascii=False, indent=2)
os.chmod(path, 0o600)
PY
chown -R liverec:liverec "$DATA"

step "[5/7] เปิดพอร์ตเว็บ 80/443 ในไฟร์วอลล์ของเครื่อง"
for p in 80 443; do
  iptables -C INPUT -p tcp --dport "$p" -j ACCEPT 2>/dev/null || iptables -I INPUT 1 -p tcp --dport "$p" -j ACCEPT
done
netfilter-persistent save >/dev/null 2>&1 || true

step "[6/7] ตั้งค่าหน้าเว็บ https://$DOMAIN"
cat > /etc/caddy/Caddyfile <<EOF
$DOMAIN {
	reverse_proxy 127.0.0.1:8080
}
EOF
systemctl enable -q caddy
systemctl restart caddy

step "[7/7] ตั้งให้ทำงานตลอด 24 ชม. + อัปเดตตัวเองอัตโนมัติ"
install -m 644 "$APP/deploy/live-recorder.service" /etc/systemd/system/
install -m 644 "$APP/deploy/live-recorder-update.service" /etc/systemd/system/
install -m 644 "$APP/deploy/live-recorder-update.timer" /etc/systemd/system/
install -m 755 "$APP/deploy/liverec" /usr/local/bin/liverec
chmod +x "$APP/deploy/update.sh"
systemctl daemon-reload
systemctl enable -q --now live-recorder-update.timer
systemctl enable -q live-recorder
systemctl restart live-recorder
sleep 3

echo
if systemctl is-active -q live-recorder; then
  echo "✅ ติดตั้งเสร็จ ระบบทำงานแล้ว"
else
  echo "⚠️ ระบบยังไม่ทำงาน — ส่งข้อความนี้ให้ Claude:"; journalctl -u live-recorder -n 30 --no-pager
fi
/usr/local/bin/liverec info
