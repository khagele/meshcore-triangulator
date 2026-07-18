#!/usr/bin/env bash
# Push code changes from this bundle into the live install and restart services.
# Run on the NUC from inside an updated triangulator-nuc/ folder:  sudo ./update.sh
#
# Safe to re-run: never touches config.ini, the database, or the venv data.
set -euo pipefail

APP_DIR=/opt/meshcore-triangulator
APP_USER=mctri
SRC_DIR="$(cd "$(dirname "$0")" && pwd)"

if [ ! -d "$APP_DIR" ]; then
  echo "[!] $APP_DIR not found — run ./install.sh first for a fresh install." >&2
  exit 1
fi

echo "==> Syncing code to $APP_DIR (keeping config.ini, database, venv)"
rsync -a --delete \
  --exclude 'config.ini' --exclude '*.db' --exclude '*.db-*' \
  --exclude '.venv' --exclude '__pycache__' --exclude 'triangulator-targets.json' \
  --exclude 'validate-report.txt' --exclude 'validate-history.log' \
  "$SRC_DIR"/ "$APP_DIR"/

echo "==> Updating Python dependencies (if any changed)"
"$APP_DIR/.venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"

echo "==> Refreshing systemd unit files"
cp "$SRC_DIR"/meshcore-*.service "$SRC_DIR"/meshcore-*.timer /etc/systemd/system/
systemctl daemon-reload

chown -R "$APP_USER:$APP_USER" "$APP_DIR"

echo "==> Restarting long-running services"
systemctl restart meshcore-triangulator   # collector
systemctl restart meshcore-map             # web frontend (picks up server.py changes)
# Timers + oneshot jobs (export / validate / prune) pick up new code on their next run.

cat <<EOF

Update done.
  Collector:  systemctl status meshcore-triangulator
  Web map:    http://<nuc-ip>:8000   (hard-refresh the browser: Ctrl/Cmd+Shift+R)
  Re-run an export now:  systemctl start meshcore-export
EOF
