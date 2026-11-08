#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

test -f .env || { echo "缺少 .env" >&2; exit 1; }
if [[ -z "$(docker compose ps -q bot)" ]]; then
  echo "bot 容器不存在，请先启动 bot" >&2
  exit 1
fi

docker compose exec -T bot python - "${1:-}" "${2:-}" <<'PY'
from __future__ import annotations

import asyncio
import os
import sys
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from pwcs2_bot.ai_chat import AIChatService
from pwcs2_bot.daily_summary import DailySummaryService
from pwcs2_bot.database import BindingRepository
from pwcs2_bot.perfectworld import PerfectWorldClient


async def discard(_group_id: str, _content: str) -> None:
    pass


async def main() -> None:
    repository = BindingRepository(os.environ.get("PWCS2_DATABASE_PATH", "/data/bindings.db"))
    bindings = repository.list_bindings()
    if not bindings:
        raise RuntimeError("数据库中没有可用于每日群报验收的绑定")
    group_id = sys.argv[1].strip() or bindings[0].group_id
    requested_date = sys.argv[2].strip()
    summary_date = (
        date.fromisoformat(requested_date)
        if requested_date
        else datetime.now(ZoneInfo("Asia/Shanghai")).date() - timedelta(days=1)
    )
    api = PerfectWorldClient(
        os.environ.get("PERFECTWORLD_TOKEN", ""),
        my_steam_id=os.environ.get("PERFECTWORLD_MY_STEAM_ID", ""),
    )
    ai = AIChatService(
        os.environ.get("USTC_LLM_API_KEY", ""),
        base_url="https://api.llm.ustc.edu.cn/v1",
        proxy_url=os.environ.get("USTC_LLM_PROXY_URL", ""),
        model=os.environ.get("USTC_LLM_MODEL", "deepseek-flash"),
        vision_model=os.environ.get("USTC_LLM_VISION_MODEL", "deepseek-flash"),
        timeout_seconds=120,
        cooldown_seconds=0,
    )
    service = DailySummaryService(repository, api, ai, discard)
    try:
        summary = await service.collect_group(group_id, summary_date)
        if not summary.members:
            print("OK daily summary query succeeded; no bound member played on that date")
        else:
            content = await service.generate_summary(summary)
            if summary_date.isoformat() not in content:
                raise RuntimeError("每日群报未包含统计日期")
            print("OK daily summary queried and generated without sending to QQ")
            print(f"activeMembers={len(summary.members)}")
            print(f"uniqueMatches={summary.unique_matches}")
            print(f"preview={content[:300]}")
        print(f"group={group_id}")
        print(f"date={summary_date.isoformat()}")
    finally:
        await ai.close()
        await api.close()


asyncio.run(main())
PY
