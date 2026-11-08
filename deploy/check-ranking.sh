#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

test -f .env || { echo "缺少 .env" >&2; exit 1; }
if ! grep -Eq '^PERFECTWORLD_TOKEN=.+$' .env; then
  echo "PERFECTWORLD_TOKEN 未配置" >&2
  exit 1
fi
if [[ -z "$(docker compose ps -q bot)" ]]; then
  echo "bot 容器不存在，请先运行 docker compose up -d bot" >&2
  exit 1
fi
REQUESTED_GROUP="${1:-}"
SOURCE_SELECTOR="${2:-}"
if [[ "${REQUESTED_GROUP,,}" == "gp" && -z "$SOURCE_SELECTOR" ]]; then
  SOURCE_SELECTOR="gp"
  REQUESTED_GROUP=""
fi
if [[ -n "$SOURCE_SELECTOR" && "${SOURCE_SELECTOR,,}" != "gp" ]]; then
  echo "用法: $0 [群号] [gp]（省略群号时可直接传 gp）" >&2
  exit 1
fi

docker compose exec -T bot python - "$REQUESTED_GROUP" "$SOURCE_SELECTOR" <<'PY'
from __future__ import annotations

import asyncio
import os
import struct
import sys

from pwcs2_bot.commands import CommandService, ParsedCommand
from pwcs2_bot.database import BindingRepository
from pwcs2_bot.perfectworld import PerfectWorldClient
from pwcs2_bot.stats_card import StatsCardRenderer


async def main() -> None:
    repository = BindingRepository(os.environ.get("PWCS2_DATABASE_PATH", "/data/bindings.db"))
    requested_group = sys.argv[1].strip()
    source = "official" if sys.argv[2].casefold() == "gp" else "pvp"
    bindings = repository.list_bindings()
    if not bindings:
        raise RuntimeError("数据库中没有可用于排行榜验收的绑定")
    group_id = requested_group or bindings[0].group_id

    client = PerfectWorldClient(
        os.environ.get("PERFECTWORLD_TOKEN", ""),
        my_steam_id=os.environ.get("PERFECTWORLD_MY_STEAM_ID", ""),
    )
    renderer = StatsCardRenderer(timeout_seconds=30)
    path = None
    try:
        service = CommandService(repository, client, query_cooldown_seconds=0)
        result = await service.handle_result(
            ParsedCommand("ranking", "gp" if source == "official" else "急停"),
            group_id=group_id,
            qq_id="acceptance",
        )
        if result.ranking is None:
            raise RuntimeError(result.content)
        assert result.ranking.entries
        assert result.ranking.source == source
        if source == "pvp":
            assert any(entry.rapid_stop is not None for entry in result.ranking.entries)
        path = await renderer.render_ranking(result.ranking)
        payload = path.read_bytes()
        assert payload[:8] == b"\x89PNG\r\n\x1a\n"
        assert payload[12:16] == b"IHDR"
        width, height = struct.unpack(">II", payload[16:24])
        assert width >= 1000 and height >= 600
        print(f"OK {source} group ranking queried and card rendered")
        print(f"source={source}")
        print(f"group={group_id}")
        print(f"entries={len(result.ranking.entries)}")
        print(f"failed={result.ranking.failed_count}")
    finally:
        if path is not None:
            path.unlink(missing_ok=True)
        await renderer.close()
        await client.close()


asyncio.run(main())
PY
