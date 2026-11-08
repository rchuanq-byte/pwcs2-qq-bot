#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if [[ -z "$(docker compose ps -q bot)" ]]; then
  echo "bot 容器不存在，请先启动 bot" >&2
  exit 1
fi

docker compose exec -T bot python - <<'PY'
from __future__ import annotations

import asyncio
from pathlib import Path

from pwcs2_bot.teamspeak import TeamSpeakStatusClient, format_teamspeak_status


async def main() -> None:
    password = Path("/run/secrets/ts3_query_password").read_text(
        encoding="utf-8"
    ).strip()
    client = TeamSpeakStatusClient(
        host="teamspeak-query",
        port=10011,
        username="serveradmin",
        password=password,
        timeout_seconds=5,
        cache_ttl_seconds=0,
    )
    status = await client.status()
    rendered = format_teamspeak_status(
        status,
        address="ts.example.com",
    )
    if not rendered.startswith("TS3 实时状态｜在线 "):
        raise RuntimeError("TeamSpeak status formatter returned an invalid message")
    print("OK TeamSpeak internal query and formatter succeeded")
    print(f"online={status.online_clients}")
    print(f"max={status.max_clients}")
    print(f"channels={len(status.channels)}")


asyncio.run(main())
PY
