#!/usr/bin/env bash
# Pull the latest code. The recorder restarts itself only when nothing is being recorded.
set -euo pipefail
APP=/opt/live-recorder
DATA=/var/lib/live-recorder
cd "$APP"
old=$(git rev-parse HEAD)
git fetch -q origin main
git reset -q --hard origin/main
new=$(git rev-parse HEAD)
if [ "$old" != "$new" ]; then
  echo "updated $old -> $new"
  install -m 644 deploy/live-recorder.service /etc/systemd/system/
  install -m 644 deploy/live-recorder-update.service /etc/systemd/system/
  install -m 644 deploy/live-recorder-update.timer /etc/systemd/system/
  install -m 755 deploy/liverec /usr/local/bin/liverec
  chmod +x deploy/update.sh
  systemctl daemon-reload
  if systemctl is-active -q live-recorder; then
    touch "$DATA/restart-pending" && chown liverec:liverec "$DATA/restart-pending"
  else
    systemctl restart live-recorder
  fi
fi
