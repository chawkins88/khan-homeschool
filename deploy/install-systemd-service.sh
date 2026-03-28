#!/usr/bin/env bash
set -euo pipefail

SERVICE_NAME="khan-homeschool.service"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE_SRC="$REPO_ROOT/deploy/$SERVICE_NAME"
SERVICE_DST="/etc/systemd/system/$SERVICE_NAME"

if [[ ! -f "$SERVICE_SRC" ]]; then
  echo "Missing service file: $SERVICE_SRC" >&2
  exit 1
fi

sudo install -m 0644 "$SERVICE_SRC" "$SERVICE_DST"
sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE_NAME"
sudo systemctl restart "$SERVICE_NAME"
sudo systemctl --no-pager --full status "$SERVICE_NAME"

echo
echo "Dashboard should be available at: http://$(hostname -I | awk '{print $1}'):8008/"
