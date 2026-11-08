#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CLIENT="${IWAN_CLIENT:-/usr/local/bin/iwan-client-oidc}"
CONFIG_DIR="${IWAN_CONFIG_DIR:-$HOME/.config/iwan}"
ENV_PATH="/etc/qqbot-iwan.env"
PROXY_DOMAINS="${IWAN_PROXY_DOMAINS:-api.llm.ustc.edu.cn}"
TUN_NAME="${IWAN_TUN:-iwan0}"
TUN_MTU="${IWAN_MTU:-1280}"

if [[ "${EUID}" -eq 0 ]]; then
  echo "Run this script as the normal server user, not root. It will use sudo only for systemd." >&2
  exit 1
fi
test -x "$CLIENT" || { echo "$CLIENT not found. Run: sudo bash deploy/install_ustc_iwan.sh" >&2; exit 1; }

mkdir -p "$CONFIG_DIR"
chmod 700 "$CONFIG_DIR"

if [[ ! -s "$CONFIG_DIR/servers.json" ]]; then
  echo "No local iWAN OIDC config found. Starting USTC unified authentication fetch."
  echo "Open the printed URL in a browser, finish login, then paste the com.panabit.mobile:// callback URL here."
  "$CLIENT" --fetch --config-dir "$CONFIG_DIR"
fi

echo
echo "Available iWAN lines:"
"$CLIENT" --list --config-dir "$CONFIG_DIR"
echo
LINE="${IWAN_LINE:-}"
if [[ -z "$LINE" ]]; then
  read -r -p "Line number to use for the always-on service [1]: " LINE
else
  echo "Using iWAN line from IWAN_LINE: $LINE"
fi
LINE="${LINE:-1}"
if ! [[ "$LINE" =~ ^[0-9]+$ ]] || [[ "$LINE" -le 0 ]]; then
  echo "Invalid line number: $LINE" >&2
  exit 1
fi

# Refresh/reselect the active SOCKS deployment without accidentally reinstalling
# the retired TUN service or its watchdog.
if [[ -f /etc/systemd/system/qqbot-campus-proxy.service ]]; then
  chmod 0600 "$CONFIG_DIR/servers.json"
  sudo sed -i "s/^IWAN_LINE=.*/IWAN_LINE=$LINE/" /etc/qqbot-campus-proxy.env
  sudo systemctl restart qqbot-campus-proxy.service
  echo "OK campus SOCKS service restarted; legacy TUN was not enabled."
  echo "Next: $ROOT_DIR/deploy/check-iwan.sh"
  exit 0
fi

sudo tee "$ENV_PATH" >/dev/null <<EOF
IWAN_LINE=$LINE
IWAN_PROXY_DOMAINS=$PROXY_DOMAINS
IWAN_TUN=$TUN_NAME
IWAN_MTU=$TUN_MTU
IWAN_CONFIG_DIR=$CONFIG_DIR
EOF
sudo chmod 0644 "$ENV_PATH"
sudo bash "$ROOT_DIR/deploy/install_iwan_service_units.sh"
sudo systemctl restart qqbot-iwan.service

sleep 5
sudo systemctl status qqbot-iwan.service --no-pager -l || true

echo
echo "Next verification:"
echo "  $ROOT_DIR/deploy/check-iwan.sh"
echo "  $ROOT_DIR/deploy/check-ai.sh"
