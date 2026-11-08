#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

grep -qE '^STEAM_WEB_API_KEY=[A-Fa-f0-9]{32}$' .env || {
  echo "STEAM_WEB_API_KEY 未配置，请先运行 python3 deploy/configure_steam.py" >&2
  exit 1
}

docker compose exec -T bot python - <<'PY'
from __future__ import annotations

import asyncio
import os

from pwcs2_bot.steam_presence import SteamPresenceClient


async def main() -> None:
    steam_id = os.environ.get("PERFECTWORLD_MY_STEAM_ID", "").strip()
    if not steam_id.isdigit():
        steam_id = "76561197960435530"
    client = SteamPresenceClient(
        os.environ.get("STEAM_WEB_API_KEY", ""),
        proxy_url=os.environ.get("STEAM_PRESENCE_PROXY_URL", ""),
    )
    try:
        result = await client.summaries([steam_id])
    finally:
        await client.close()
    print(f"OK Steam presence query succeeded; players={len(result)}")


asyncio.run(main())
PY
