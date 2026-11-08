#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

STEAM_ID="${1:-}"
SOURCE_SELECTOR="${2:-}"
if [[ ! "$STEAM_ID" =~ ^7656119[0-9]{10}$ ]]; then
  echo "用法: $0 <SteamID64> [gp]" >&2
  exit 1
fi
if [[ -n "$SOURCE_SELECTOR" && "${SOURCE_SELECTOR,,}" != "gp" ]]; then
  echo "数据源只支持省略（完美）或 gp（官匹）" >&2
  exit 1
fi
if [[ -z "$(docker compose ps -q bot)" ]]; then
  echo "bot 容器不存在，请先启动 bot" >&2
  exit 1
fi

docker compose exec -T bot python - "$STEAM_ID" "$SOURCE_SELECTOR" <<'PY'
from __future__ import annotations

import asyncio
import os
import struct
import sys

from pwcs2_bot.game_identity import resolve_game_profile
from pwcs2_bot.match_formatting import build_match_history
from pwcs2_bot.perfectworld import PerfectWorldApiError, PerfectWorldClient
from pwcs2_bot.stats_card import StatsCardRenderer


async def main() -> None:
    steam_id = sys.argv[1]
    source = "official" if sys.argv[2].casefold() == "gp" else "pvp"
    client = PerfectWorldClient(
        os.environ.get("PERFECTWORLD_TOKEN", ""),
        my_steam_id=os.environ.get("PERFECTWORLD_MY_STEAM_ID", ""),
    )
    renderer = StatsCardRenderer(timeout_seconds=30)
    path = None
    try:
        if source == "official":
            matches = await client.official_match_list(steam_id, page_size=10)
            profile = await resolve_game_profile(client, steam_id, matches, source=source)
        else:
            matches = await client.match_list(steam_id, page_size=10)
            try:
                profile = await client.detail_stats(steam_id)
            except PerfectWorldApiError:
                profile = {}
        history = build_match_history(
            matches,
            steam_id=steam_id,
            profile=profile,
            source=source,
        )
        path = await renderer.render_match_history(history)
        payload = path.read_bytes()
        assert payload[:8] == b"\x89PNG\r\n\x1a\n"
        assert payload[12:16] == b"IHDR"
        width, height = struct.unpack(">II", payload[16:24])
        assert width >= 1000 and height >= 600
        source_label = "official" if source == "official" else "Perfect World"
        print(f"OK {source_label} recent ten-match history queried and card rendered")
        print(f"source={source}")
        print(f"name={history.name}")
        print(f"matches={history.total}")
        print(f"record={history.wins}-{history.losses}-{history.draws}")
        print(f"eloChange={history.elo_change}")
    finally:
        if path is not None:
            path.unlink(missing_ok=True)
        await renderer.close()
        await client.close()


asyncio.run(main())
PY
