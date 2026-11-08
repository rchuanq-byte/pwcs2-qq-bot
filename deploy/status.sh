#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

ok() {
  echo "OK   $*"
}

warn() {
  echo "WARN $*"
}

fail() {
  echo "FAIL $*"
}

configured_flag() {
  local key="$1"
  python3 - "$key" <<'PY'
import sys
from pathlib import Path

key = sys.argv[1]
env_path = Path(".env")
values: dict[str, str] = {}

if env_path.exists():
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[name.strip()] = value

value = values.get(key, "")
print(str(bool(value and not value.startswith("CHANGE_ME"))).lower())
PY
}

echo "== Git =="
if [[ -d .git ]]; then
  ok "repo: $(git rev-parse --show-toplevel)"
  ok "branch: $(git branch --show-current)"
  ok "commit: $(git rev-parse --short HEAD)"
  if [[ -z "$(git status --porcelain --untracked-files=normal)" ]]; then
    ok "worktree clean"
  else
    warn "worktree has uncommitted changes"
    git status --short --untracked-files=normal
  fi
else
  fail "not a Git repository"
fi

echo
echo "== Secrets =="
if [[ -f .env ]]; then
  ok ".env exists"
  [[ "$(configured_flag NAPCAT_ONEBOT_TOKEN)" == "true" ]] \
    && ok "NAPCAT_ONEBOT_TOKEN configured" \
    || fail "NAPCAT_ONEBOT_TOKEN missing or placeholder"
  [[ "$(configured_flag PERFECTWORLD_TOKEN)" == "true" ]] \
    && ok "PERFECTWORLD_TOKEN configured" \
    || warn "PERFECTWORLD_TOKEN missing or placeholder"
  [[ "$(configured_flag PERFECTWORLD_MY_STEAM_ID)" == "true" ]] \
    && ok "PERFECTWORLD_MY_STEAM_ID configured" \
    || ok "PERFECTWORLD_MY_STEAM_ID not configured; using queried SteamID as mySteamId"
else
  fail ".env missing"
fi

echo
echo "== Docker Compose =="
docker compose ps

echo
echo "== Bot health =="
if docker compose exec -T --interactive=false bot python -m pwcs2_bot.healthcheck; then
  ok "gateway responsive and active QQ connection recently verified online"
else
  fail "gateway or QQ connection not ready; see safe error code above"
fi

echo
echo "== OneBot WebSocket =="
if docker compose exec -T bot python - <<'PY'
import base64
import os
import socket
import sys

token = os.environ.get("NAPCAT_ONEBOT_TOKEN", "")
key = base64.b64encode(os.urandom(16)).decode()
request = (
    "GET / HTTP/1.1\r\n"
    "Host: napcat:3001\r\n"
    "Upgrade: websocket\r\n"
    "Connection: Upgrade\r\n"
    f"Sec-WebSocket-Key: {key}\r\n"
    "Sec-WebSocket-Version: 13\r\n"
    f"Authorization: Bearer {token}\r\n"
    "\r\n"
)

try:
    with socket.create_connection(("napcat", 3001), timeout=3) as sock:
        sock.sendall(request.encode("ascii"))
        response = sock.recv(4096).decode("latin1", errors="replace")
except OSError as exc:
    print(f"connect failed: {exc}")
    sys.exit(1)

status_line = response.splitlines()[0] if response else "NO_RESPONSE"
print(status_line)
sys.exit(0 if status_line.startswith("HTTP/1.1 101 ") else 1)
PY
then
  ok "OneBot handshake works (QQ login state is checked separately above)"
else
  fail "bot cannot authenticate to NapCat OneBot WebSocket"
fi
