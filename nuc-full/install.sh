#!/usr/bin/env bash
# Install meshcore_mqtt_triangulator on a Linux NUC (Ubuntu/Debian, x86-64).
# Run from inside the triangulator-nuc/ folder:  sudo ./install.sh
set -euo pipefail

APP_DIR=/opt/meshcore-triangulator
APP_USER=mctri
SRC_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "==> Installing system packages"
apt-get update -qq
apt-get install -y python3 python3-venv python3-pip

echo "==> Creating service user '$APP_USER'"
id -u "$APP_USER" >/dev/null 2>&1 || useradd --system --create-home --shell /usr/sbin/nologin "$APP_USER"

echo "==> Copying app to $APP_DIR"
mkdir -p "$APP_DIR"
# Copy code but never clobber an existing config.ini or database
rsync -a --exclude 'config.ini' --exclude '*.db' --exclude '*.db-*' --exclude '.venv' \
      "$SRC_DIR"/ "$APP_DIR"/

echo "==> Building virtualenv"
python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install --upgrade pip -q
"$APP_DIR/.venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"

echo "==> Seeding config.ini (if missing)"
if [ ! -f "$APP_DIR/config.ini" ]; then
  cp "$APP_DIR/config.example.ini" "$APP_DIR/config.ini"
  echo "    -> EDIT $APP_DIR/config.ini and set your broker host/auth before starting."
fi

chown -R "$APP_USER:$APP_USER" "$APP_DIR"

echo "==> Installing systemd units"
cp "$SRC_DIR/meshcore-triangulator.service" /etc/systemd/system/meshcore-triangulator.service
cp "$SRC_DIR/meshcore-export.service"       /etc/systemd/system/meshcore-export.service
cp "$SRC_DIR/meshcore-export.timer"         /etc/systemd/system/meshcore-export.timer
cp "$SRC_DIR/meshcore-validate.service"     /etc/systemd/system/meshcore-validate.service
cp "$SRC_DIR/meshcore-validate.timer"       /etc/systemd/system/meshcore-validate.timer
cp "$SRC_DIR/meshcore-map.service"          /etc/systemd/system/meshcore-map.service
cp "$SRC_DIR/meshcore-prune.service"        /etc/systemd/system/meshcore-prune.service
cp "$SRC_DIR/meshcore-prune.timer"          /etc/systemd/system/meshcore-prune.timer
systemctl daemon-reload
# Enable the hourly export timer (writes web/triangulator-targets.json each hour)
# and the daily accuracy self-check (writes validate-report.txt).
systemctl enable --now meshcore-export.timer
systemctl enable --now meshcore-validate.timer
# Enable the web map frontend (mc-map 2) on port 8000.
systemctl enable --now meshcore-map.service
# Weekly retention prune (deletes observations older than 45 days).
systemctl enable --now meshcore-prune.timer

cat <<EOF

Done. Next steps:
  1. sudo nano $APP_DIR/config.ini          # set broker host, port, auth, freq_mhz
  2. (optional) copy your existing DB:
       sudo systemctl stop meshcore-triangulator   # if already running
       scp meshcore_data.db  this-nuc:$APP_DIR/    # from the source machine
       sudo chown $APP_USER:$APP_USER $APP_DIR/meshcore_data.db
  3. sudo systemctl enable --now meshcore-triangulator
  4. journalctl -u meshcore-triangulator -f       # watch it collect

Hourly location export is already enabled (meshcore-export.timer).
  - Output:        $APP_DIR/triangulator-targets.json   (refreshed every hour)
  - Run it now:    sudo systemctl start meshcore-export
  - Watch it:      journalctl -u meshcore-export -f
  - Next run:      systemctl list-timers meshcore-export.timer

Locate from the NUC (ad hoc):
  sudo -u $APP_USER $APP_DIR/.venv/bin/python $APP_DIR/targets.py
  sudo -u $APP_USER $APP_DIR/.venv/bin/python $APP_DIR/locate.py --target <pubkey-prefix>

Filter the export by node name (e.g. only "maaskern" nodes):
  sudo -u $APP_USER $APP_DIR/.venv/bin/python $APP_DIR/export_triangulator.py \\
       --repo $APP_DIR --out $APP_DIR/maaskern.json --name maaskern

Web map (mc-map 2) is running on this NUC:
  Open  http://<nuc-ip>:8000  in a browser on your network.
  The "Triangulator overlay" panel augments/corrects mc-radar & meshcore.io
  nodes with your localiser estimates (web/triangulator-targets.json).
  Tip: run the first export now so the map has data:  sudo systemctl start meshcore-export
EOF
