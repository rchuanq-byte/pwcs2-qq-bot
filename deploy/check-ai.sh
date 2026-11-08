#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

test -f .env || { echo "缺少 .env" >&2; exit 1; }
if ! grep -Eq '^USTC_LLM_API_KEY=.+$' .env; then
  echo "USTC_LLM_API_KEY 未配置，请先运行 python3 deploy/configure_ai.py" >&2
  exit 1
fi
if [[ -z "$(docker compose ps -q bot)" ]]; then
  echo "bot 容器不存在，请先启动 bot" >&2
  exit 1
fi

docker compose exec -T bot python - <<'PY'
from __future__ import annotations

import asyncio
import base64
import binascii
import os
import struct
import zlib

from pwcs2_bot.ai_chat import AIChatService


def red_png_data_url() -> str:
    def chunk(kind: bytes, data: bytes) -> bytes:
        checksum = binascii.crc32(kind + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", checksum)

    width = height = 256
    rows = b"".join(b"\x00" + b"\xff\x00\x00" * width for _ in range(height))
    png = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(rows))
        + chunk(b"IEND", b"")
    )
    return "data:image/png;base64," + base64.b64encode(png).decode("ascii")


async def main() -> None:
    service = AIChatService(
        os.environ.get("USTC_LLM_API_KEY", ""),
        base_url="https://api.llm.ustc.edu.cn/v1",
        proxy_url=os.environ.get("USTC_LLM_PROXY_URL", ""),
        model=os.environ.get("USTC_LLM_MODEL", "deepseek-flash"),
        vision_model=os.environ.get("USTC_LLM_VISION_MODEL", "deepseek-flash"),
        cooldown_seconds=0,
        timeout_seconds=None,
    )
    try:
        text_answer = await service.chat(
            chat_id="acceptance",
            requester_id="acceptance",
            content="只回复两个大写字母 OK，不要添加其他内容。",
        )
        image_answer = await service.chat(
            chat_id="acceptance-image",
            requester_id="acceptance",
            content="What color is the attached image? Answer with one English color word only.",
            image_data_urls=(red_png_data_url(),),
        )
    finally:
        await service.close()
    if "OK" not in text_answer.upper():
        raise RuntimeError("DeepSeek 文字主模型返回内容不符合验收预期")
    if "red" not in image_answer.lower():
        raise RuntimeError("图片识别返回内容不符合验收预期")
    route = "native multimodal" if service.model == service.vision_model else "vision helper"
    print(f"OK AI text and {route} image routing succeeded; model={service.model}")


asyncio.run(main())
PY
