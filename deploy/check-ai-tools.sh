#!/usr/bin/env bash
# Synthetic integration check only: no production group data or QQ messages.
set -Eeuo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
docker compose exec -T bot python - <<'PY'
import asyncio
import os
import sqlite3
import tempfile
from pathlib import Path

from pwcs2_bot.ai_chat import AIChatService
from pwcs2_bot.ai_group_data import AIGroupDataService
from pwcs2_bot.commands import CommandService
from pwcs2_bot.community import CommunityRepository, GroupPlayService
from pwcs2_bot.database import BindingRepository


async def main():
    with tempfile.TemporaryDirectory(prefix="ai-tools-acceptance-") as temp:
        path = Path(temp) / "synthetic.db"
        bindings = BindingRepository(path)
        community = CommunityRepository(path)
        bindings.upsert_group_member_identity("test-group", "100001", "验收用户")
        community.wallet("test-group", "100001")
        with sqlite3.connect(path) as conn:
            conn.execute("UPDATE coin_wallets SET balance=1742 WHERE group_id=? AND qq_id=?",
                         ("test-group", "100001"))
        service = AIGroupDataService(CommandService(
            bindings, None, group_play=GroupPlayService(community, bindings),
        ))
        toolbox = service.for_request("test-group", "100001")

        class ObservedTools:
            definitions = toolbox.definitions
            context = toolbox.context

            def __init__(self):
                self.successful_queries = 0

            async def execute(self, name, arguments):
                result = await toolbox.execute(name, arguments)
                if name == "query_group_statistics" and result.get("ok"):
                    self.successful_queries += 1
                return result

        observed = ObservedTools()
        ai = AIChatService(
            os.environ.get("USTC_LLM_API_KEY", ""),
            base_url="https://api.llm.ustc.edu.cn/v1",
            proxy_url=os.environ.get("USTC_LLM_PROXY_URL", ""),
            model=os.environ.get("USTC_LLM_MODEL", "deepseek-flash"),
            vision_model=os.environ.get("USTC_LLM_VISION_MODEL", "deepseek-flash"),
            cooldown_seconds=0, timeout_seconds=60,
        )
        try:
            async with asyncio.timeout(180):
                answer = await ai.chat(chat_id="acceptance-tools", requester_id="100001",
                                       content="查一下我的牛币余额，只回答数值，不要猜测。",
                                       toolbox=observed)
            if not observed.successful_queries or "1742" not in answer.replace(",", ""):
                raise RuntimeError("tool round trip did not return verified synthetic balance")
            print(f"OK native AI statistics tool round trip; model={ai.model}; "
                  f"successful_queries={observed.successful_queries}; no QQ message sent")
        finally:
            await ai.close()


asyncio.run(main())
PY
