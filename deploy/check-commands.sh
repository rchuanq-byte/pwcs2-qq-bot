#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if [[ -z "$(docker compose ps -q bot)" ]]; then
  echo "bot 容器不存在，请先运行 docker compose up -d bot" >&2
  exit 1
fi

docker compose exec -T bot python - <<'PY'
from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from pwcs2_bot.commands import CommandService
from pwcs2_bot.database import BindingRepository
from pwcs2_bot.perfectworld import PerfectWorldClient
from pwcs2_bot.query_source import parse_source_argument


STEAM_ID = "76561198929215155"


async def main() -> int:
    with tempfile.TemporaryDirectory(prefix="pwcs2-command-check-") as temp_dir:
        database_path = Path(temp_dir) / "bindings.db"
        repository = BindingRepository(database_path)
        api = PerfectWorldClient("")
        service = CommandService(repository, api, query_cooldown_seconds=0)

        try:
            help_command = service.parse("#帮助")
            assert help_command is not None and help_command.action == "help"
            help_reply = await service.handle(help_command, group_id="1000", qq_id="2000")
            assert "#绑定 <SteamID64>" in help_reply
            assert "#瓦绑定 <昵称#数字ID>" in help_reply
            assert "#瓦战绩" in help_reply
            assert "默认完美" in help_reply
            assert "#战绩 [gp]" in help_reply

            bind_command = service.parse(f"#绑定 {STEAM_ID}")
            assert bind_command is not None and bind_command.action == "bind"
            bind_reply = await service.handle(bind_command, group_id="1000", qq_id="2000")
            assert STEAM_ID in bind_reply

            binding_command = service.parse("#我的绑定")
            assert binding_command is not None and binding_command.action == "binding"
            binding_reply = await service.handle(binding_command, group_id="1000", qq_id="2000")
            assert STEAM_ID in binding_reply

            valorant_bind = service.parse("#瓦绑定 ExamplePlayer#12345")
            assert valorant_bind is not None and valorant_bind.action == "valorant_bind"
            valorant_reply = await service.handle(
                valorant_bind,
                group_id="1000",
                qq_id="2000",
            )
            assert "ExamplePlayer#12345" in valorant_reply

            valorant_binding = service.parse("#我的瓦绑定")
            assert valorant_binding is not None
            assert valorant_binding.action == "valorant_binding"
            valorant_binding_reply = await service.handle(
                valorant_binding,
                group_id="1000",
                qq_id="2000",
            )
            assert "ExamplePlayer#12345" in valorant_binding_reply

            data_command = service.parse("#数据")
            assert data_command is not None and data_command.action == "stats"
            history_command = service.parse("#战绩")
            assert history_command is not None and history_command.action == "matches"
            assert parse_source_argument("").source == "pvp"
            official_history = service.parse("#战绩 gp")
            assert official_history is not None and official_history.action == "matches"
            official_target = parse_source_argument(f"gp {STEAM_ID}")
            assert official_target.source == "official"
            assert official_target.argument == STEAM_ID

            unbind_command = service.parse("#解绑")
            assert unbind_command is not None and unbind_command.action == "unbind"
            unbind_reply = await service.handle(unbind_command, group_id="1000", qq_id="2000")
            assert "解绑成功" in unbind_reply
        finally:
            await api.close()

    print("OK deterministic command smoke test passed")
    return 0


raise SystemExit(asyncio.run(main()))
PY
