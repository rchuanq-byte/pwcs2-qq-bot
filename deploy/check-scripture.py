#!/usr/bin/env python3
"""Verify packaged scripture and optionally one short live AI citation, without QQ output."""

from __future__ import annotations

import argparse
import asyncio
import os

from pwcs2_bot.ai_chat import AIChatService
from pwcs2_bot.niuniu_scripture import SCRIPTURE_GROUP_ID, reference_context, scripture_library


async def main(live_ai: bool) -> None:
    library = scripture_library()
    verse = library.by_key["MRK 10:43"]
    context = reference_context(f"group:{SCRIPTURE_GROUP_ID}", "引用马可卷10:43")
    assert verse.text in context and len(library.verses) == 2029
    print(f"OK scripture_corpus verses={len(library.verses)} reference=MRK10:43")
    if not live_ai:
        return
    ai = AIChatService(
        os.environ.get("USTC_LLM_API_KEY", ""),
        proxy_url=os.environ.get("USTC_LLM_PROXY_URL", ""),
        model=os.environ.get("USTC_LLM_MODEL", "deepseek-flash"),
        vision_model=os.environ.get("USTC_LLM_VISION_MODEL", "deepseek-flash"),
        cooldown_seconds=0, max_tokens=220, timeout_seconds=55,
    )
    try:
        answer = await ai.chat(
            chat_id=f"group:{SCRIPTURE_GROUP_ID}", requester_id="scripture-acceptance",
            content="请逐字引用牛牛圣经马可卷10:43，只给群译经文和出处，不要解释。",
        )
    finally:
        await ai.close()
    assert verse.text in answer and "10:43" in answer, "AI did not return the exact verified quote"
    print("OK live_ai_exact_citation")
    print(answer)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live-ai", action="store_true")
    try:
        asyncio.run(main(parser.parse_args().live_ai))
    except Exception as exc:
        print(f"FAIL scripture_check {type(exc).__name__}")
        raise SystemExit(1) from None
