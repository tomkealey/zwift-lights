#!/usr/bin/env bash
# Installs zwiftlights on a Raspberry Pi and sets it up as a background service.
# Run from inside the zwiftlights folder:  bash install.sh
set -euo pipefail
cd "$(dirname "$0")"
DIR="$(pwd)"

echo "==> Installing system packages"
sudo apt-get update -qq
sudo apt-get install -y -qq python3-venv bluez

echo "==> Making sure $USER can use Bluetooth"
sudo usermod -aG bluetooth "$USER"

echo "==> Creating Python environment (takes a few minutes on a Pi 3)"
python3 -m venv venv
venv/bin/pip install --quiet --upgrade pip
venv/bin/pip install --quiet bleak

echo "==> Installing the background service"
sed -e "s|__USER__|$USER|g" -e "s|__DIR__|$DIR|g" zwiftlights.service \
  | sudo tee /etc/systemd/system/zwiftlights.service > /dev/null
sudo systemctl daemon-reload

cat <<EOF

Done. Next steps (once the Hue bridge is set up in the Hue app):

  cd $DIR
  venv/bin/python zwiftlights.py pair     # press the bridge button when asked
  venv/bin/python zwiftlights.py rooms    # check hue_room in config.toml matches
  venv/bin/python zwiftlights.py test     # lights cycle through the zone colours
  venv/bin/python zwiftlights.py scan     # pedal a bit; check the trainer shows up

Then switch it on for good (starts automatically on every boot):

  sudo systemctl enable --now zwiftlights
  journalctl -u zwiftlights -f            # watch it live (Ctrl+C to stop watching)
EOF
