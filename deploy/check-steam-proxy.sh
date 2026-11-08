#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

grep -qE '^STEAM_WEB_API_KEY=[A-Fa-f0-9]{32}$' .env || {
  echo "STEAM_WEB_API_KEY 未配置，请先运行 python3 deploy/configure_steam.py" >&2
  exit 1
}
grep -qE '^STEAM_PRESENCE_PROXY_URL=http(s)?://.+' .env || {
  echo "Steam 专用代理未配置，请先运行 python3 deploy/configure_steam_proxy.py" >&2
  exit 1
}

MIHOMO_ID="$(docker compose ps -q steam-mihomo)"
test -n "$MIHOMO_ID" || {
  echo "steam-mihomo 容器未启动" >&2
  exit 1
}
test "$(docker inspect --format='{{.State.Running}}' "$MIHOMO_ID")" = "true" || {
  echo "steam-mihomo 容器未正常运行" >&2
  docker compose logs --tail=50 steam-mihomo
  exit 1
}

docker compose exec -T bot python - <<'PY'
from __future__ import annotations

import asyncio
import os
import time

import httpx


async def main() -> None:
    proxy_url = os.environ.get("STEAM_PRESENCE_PROXY_URL", "").strip()
    api_key = os.environ.get("STEAM_WEB_API_KEY", "").strip()
    steam_id = os.environ.get("PERFECTWORLD_MY_STEAM_ID", "").strip()
    if not steam_id.isdigit():
        steam_id = "76561197960435530"

    successes = 0
    durations: list[float] = []
    async with httpx.AsyncClient(
        proxy=proxy_url,
        trust_env=False,
        timeout=httpx.Timeout(15),
        headers={"User-Agent": "pwcs2-qq-bot/0.1"},
    ) as client:
        for _ in range(10):
            started = time.monotonic()
            response = await client.get(
                "https://api.steampowered.com/ISteamNews/GetNewsForApp/v2/",
                params={"appid": "730", "count": "1"},
            )
            response.raise_for_status()
            successes += 1
            durations.append(time.monotonic() - started)

        response = await client.get(
            "https://api.steampowered.com/ISteamUser/GetPlayerSummaries/v2/",
            params={"steamids": steam_id},
            headers={"x-webapi-key": api_key},
        )
        response.raise_for_status()
        players = response.json().get("response", {}).get("players", [])
        if not isinstance(players, list):
            raise RuntimeError("Steam 返回格式异常")

    average = sum(durations) / len(durations)
    slowest = max(durations)
    print(
        "OK Steam configured proxy passed "
        f"{successes}/10 probes; avg={average:.2f}s; max={slowest:.2f}s; "
        f"players={len(players)}"
    )


asyncio.run(main())
PY
