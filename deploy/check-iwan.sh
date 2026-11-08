#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if systemctl is-active --quiet qqbot-campus-proxy.service; then
  IWAN_PROXY_LISTEN=172.18.0.1:18081
  if [[ -r /etc/qqbot-campus-proxy.env ]]; then
    # shellcheck disable=SC1091
    source /etc/qqbot-campus-proxy.env
  fi
  code="$(curl --noproxy '' --socks5-hostname "$IWAN_PROXY_LISTEN" -sS \
    --connect-timeout 5 --max-time 15 -o /dev/null -w '%{http_code}' \
    https://api.llm.ustc.edu.cn/v1/models)"
  case "$code" in
    200|401|403) echo "OK private campus SOCKS proxy; verified TLS HTTP $code" ;;
    *) echo "FAIL campus SOCKS proxy HTTP $code" >&2; exit 1 ;;
  esac
  docker compose exec -T bot python - <<'PY'
import os
import httpx
proxy = os.environ.get('USTC_LLM_PROXY_URL', '')
assert proxy, 'USTC_LLM_PROXY_URL is not configured'
try:
    with httpx.Client(proxy=proxy, trust_env=False, timeout=15) as client:
        response = client.get('https://api.llm.ustc.edu.cn/v1/models')
    assert response.status_code in {200, 401, 403}
except Exception:
    raise SystemExit('FAIL bot private campus proxy probe') from None
print('OK bot reaches campus API through the private routing proxy')
PY
  exit 0
fi

ENV_PATH="/etc/qqbot-iwan.env"
IWAN_TUN="iwan0"
if [[ -r "$ENV_PATH" ]]; then
  # shellcheck disable=SC1090
  source "$ENV_PATH"
fi
IWAN_TUN="${IWAN_TUN:-iwan0}"
IWAN_PROXY_DOMAINS="${IWAN_PROXY_DOMAINS:-api.llm.ustc.edu.cn}"
IWAN_MTU="${IWAN_MTU:-1280}"

if ! systemctl is-active --quiet qqbot-iwan.service; then
  echo "qqbot-iwan.service is not active" >&2
  systemctl status qqbot-iwan.service --no-pager -l || true
  exit 1
fi

ip link show "$IWAN_TUN" >/dev/null || { echo "TUN device $IWAN_TUN is missing" >&2; exit 1; }
ACTUAL_MTU="$(cat "/sys/class/net/$IWAN_TUN/mtu")"
if [[ "$ACTUAL_MTU" != "$IWAN_MTU" ]]; then
  echo "TUN device $IWAN_TUN uses MTU $ACTUAL_MTU, expected $IWAN_MTU" >&2
  exit 1
fi
echo "OK TUN device $IWAN_TUN uses MTU $ACTUAL_MTU"

case ",$IWAN_PROXY_DOMAINS," in
  *,api.llm.ustc.edu.cn,*) ;;
  *)
    echo "IWAN_PROXY_DOMAINS must include api.llm.ustc.edu.cn" >&2
    exit 1
    ;;
esac

IFS=',' read -r -a PROXY_DOMAINS <<<"$IWAN_PROXY_DOMAINS"
for domain in "${PROXY_DOMAINS[@]}"; do
  domain="${domain//[[:space:]]/}"
  [[ -n "$domain" ]] || continue
  found=false
  routed=true
  while read -r address; do
    [[ -n "$address" ]] || continue
    found=true
    route="$(ip route get "$address" 2>/dev/null || true)"
    if [[ "$route" == *" dev $IWAN_TUN "* ]]; then
      echo "OK $domain address $address is routed through $IWAN_TUN"
    else
      routed=false
      echo "$domain address $address is not routed through $IWAN_TUN" >&2
    fi
  done < <(getent ahostsv4 "$domain" | awk '{print $1}' | sort -u)
  if ! "$found" || ! "$routed"; then
    echo "Not all current $domain addresses are routed through $IWAN_TUN" >&2
    exit 1
  fi
done

HOST_CODE="$(curl -sS --connect-timeout 10 --max-time 20 -o /dev/null -w '%{http_code}' https://api.llm.ustc.edu.cn/v1/models || true)"
case "$HOST_CODE" in
  200|401|403) echo "OK host can reach USTC AI endpoint through iWAN (HTTP $HOST_CODE)" ;;
  *) echo "Host cannot reach USTC AI endpoint (HTTP $HOST_CODE)" >&2; exit 1 ;;
esac

if [[ -n "$(docker compose ps -q bot 2>/dev/null || true)" ]]; then
  docker compose exec -T bot python - <<'PY'
from __future__ import annotations

import http.client
import ssl

def check(host: str, path: str, valid_statuses: set[int], label: str) -> None:
    conn = http.client.HTTPSConnection(
        host,
        port=443,
        timeout=20,
        context=ssl.create_default_context(),
    )
    try:
        conn.request("GET", path, headers={"User-Agent": "pwcs2-qq-bot/0.1"})
        response = conn.getresponse()
        response.read(1024)
        status = response.status
    finally:
        conn.close()
    if status not in valid_statuses:
        raise SystemExit(f"container cannot reach {label} endpoint (HTTP {status})")
    print(f"OK bot container can reach {label} endpoint (HTTP {status})")


check("api.llm.ustc.edu.cn", "/v1/models", {200, 401, 403}, "USTC AI")
PY
else
  echo "bot container is not running; skipped container reachability check"
fi
