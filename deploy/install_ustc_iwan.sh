#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# Legacy TUN installer. Current production uses the separate campus SOCKS service.
VERSION="${IWAN_VERSION:-v2.2.1}"
INSTALL_PATH="${IWAN_INSTALL_PATH:-/usr/local/bin/iwan-client-oidc}"
ENV_PATH="/etc/qqbot-iwan.env"

case "$(uname -m)" in
  x86_64|amd64)
    ASSET="iwan-client-oidc-x86_64-gnu.zip"
    SHA256="67861edbf6004807fa53ea684ea271c5eeb7ba06474bfb60c064d1b4158caa87"
    ;;
  *)
    echo "Unsupported architecture: $(uname -m). Update this script with the matching ustc-iwan release asset." >&2
    exit 1
    ;;
esac

URL="https://github.com/yyy1mu/ustc-iwan/releases/download/${VERSION}/${ASSET}"
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

if [[ "${EUID}" -ne 0 ]]; then
  echo "Please run with sudo: sudo bash deploy/install_ustc_iwan.sh" >&2
  exit 1
fi

MISSING_PACKAGES=()
command -v curl >/dev/null || MISSING_PACKAGES+=(curl)
command -v ip >/dev/null || MISSING_PACKAGES+=(iproute2)
command -v python3 >/dev/null || MISSING_PACKAGES+=(python3)
command -v flock >/dev/null || MISSING_PACKAGES+=(util-linux)
if [[ ! -e /dev/net/tun ]] && ! command -v modprobe >/dev/null; then
  MISSING_PACKAGES+=(kmod)
fi
if ((${#MISSING_PACKAGES[@]})); then
  apt-get update
  apt-get install -y ca-certificates "${MISSING_PACKAGES[@]}"
fi

if [[ ! -e /dev/net/tun ]]; then
  modprobe tun || true
fi
test -c /dev/net/tun || { echo "Missing /dev/net/tun; iWAN cannot create a TUN device." >&2; exit 1; }

curl -fL --retry 3 --connect-timeout 15 --max-time 120 -o "$TMP_DIR/$ASSET" "$URL"
echo "${SHA256}  $TMP_DIR/$ASSET" | sha256sum -c -
python3 - "$TMP_DIR/$ASSET" "$TMP_DIR/unpack" <<'PY'
from __future__ import annotations

import sys
import zipfile

archive, output = sys.argv[1:3]
with zipfile.ZipFile(archive) as handle:
    handle.extractall(output)
PY

BINARY="$(find "$TMP_DIR/unpack" -type f -name 'iwan-client-oidc*' | head -n 1)"
test -n "$BINARY" || { echo "Could not find iwan-client-oidc in release archive." >&2; exit 1; }
install -m 0755 "$BINARY" "$INSTALL_PATH"

if [[ ! -f "$ENV_PATH" ]]; then
  cat >"$ENV_PATH" <<'EOF'
IWAN_LINE=1
IWAN_PROXY_DOMAINS=api.llm.ustc.edu.cn
IWAN_TUN=iwan0
IWAN_MTU=1280
IWAN_CONFIG_DIR=/home/admin/.config/iwan
EOF
  chmod 0644 "$ENV_PATH"
fi

bash "$ROOT_DIR/deploy/install_iwan_service_units.sh"

echo "OK installed $INSTALL_PATH from $URL"
echo "Next: run as the normal server user, not root:"
echo "  bash deploy/configure_ustc_iwan.sh"
