"""AstrBot-owned OneBot transport for the existing CS/group application.

There is no nanobot import, gateway, message bus or second QQ WebSocket here.
AstrBot's aiocqhttp adapter is the sole owner of the reverse connection.
"""

from __future__ import annotations

import asyncio
import base64
import inspect
import ipaddress
import socket
from collections import deque
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from loguru import logger

from pwcs2_bot.channel import PWCS2Application, PWCS2Config
from pwcs2_bot.transport_contract import OutboundMessage


def _local_image(source: str, maximum: int) -> str:
    path = Path(source).resolve(strict=True)
    if not path.is_file():
        raise ValueError("Invalid outbound image")
    with path.open("rb") as stream:
        raw = stream.read(maximum + 1)
    image = raw.startswith((b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"GIF87a", b"GIF89a")) or (
        raw.startswith(b"RIFF") and raw[8:12] == b"WEBP"
    )
    if not image or not 0 < len(raw) <= maximum:
        raise ValueError("Invalid outbound image content or size")
    return "base64://" + base64.b64encode(raw).decode("ascii")


def _public_image_url(source: str) -> str:
    parsed = urlsplit(source)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        raise ValueError("Invalid image reference")
    addresses = socket.getaddrinfo(parsed.hostname, parsed.port or 443, type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(row[4][0]).is_global for row in addresses):
        raise ValueError("Image reference is not public")
    return source


class _ConnectionView:
    def __init__(self, transport):
        self.transport = None
        self.owner = transport

    async def close(self, code=1011, reason="OneBot application health probe failed"):
        # Recycle only the protocol connection. Never restart QQ or its container.
        for client in self.owner.native_clients():
            close = getattr(client, "close", None)
            if callable(close):
                result = close(code=code, reason=reason)
                if inspect.isawaitable(result):
                    await result


class AstrBotOneBotTransport:
    def __init__(self, config, bus=None):
        self.config = config
        self.bot: Any = None
        self._self_id: int | None = None
        self._processed_ids = deque(maxlen=2000)
        self._bot_outbound_ids = deque(maxlen=2000)
        self._background_tasks: set[asyncio.Task] = set()
        self._view = _ConnectionView(self)
        self._running = False
        self.sent_count = 0

    def bind_bot(self, bot, self_id: str | int):
        self.bot = bot
        self._self_id = int(self_id)

    def native_clients(self):
        if self.bot is None:
            return ()
        clients = getattr(self.bot, "_wsr_api_clients", {})
        if not isinstance(clients, dict):
            return ()
        selected = clients.get(self._self_id) or clients.get(str(self._self_id))
        return (selected,) if selected is not None else ()

    @property
    def _ws(self):
        return self._view if self.native_clients() else None

    def is_allowed(self, user_id: str) -> bool:
        return "*" in self.config.allow_from or str(user_id) in self.config.allow_from

    def _create_background_task(self, coro, kind):
        task = asyncio.create_task(coro, name="niuniu-" + kind)
        self._background_tasks.add(task)

        def done(completed):
            self._background_tasks.discard(completed)
            if not completed.cancelled():
                error = completed.exception()
                if error is not None:
                    logger.warning("niuniu: {} callback failed: {}", kind, type(error).__name__)

        task.add_done_callback(done)

    async def _call_action(self, action, params, timeout=20):  # noqa: ASYNC109
        # Timeout is part of the existing business transport's compatibility API.
        if self.bot is None or self._ws is None:
            raise RuntimeError("QQ protocol connection is not ready")
        async with asyncio.timeout(timeout):
            data = await self.bot.call_action(
                action,
                **{"self_id": self._self_id, **params},
            )
        if isinstance(data, dict) and "retcode" in data and "status" in data:
            if data["status"] != "ok" or data["retcode"] != 0:
                raise RuntimeError("QQ protocol action failed")
            return data
        return {"status": "ok", "retcode": 0, "data": data}

    @staticmethod
    def _normalize_segments(message):
        if isinstance(message, list):
            return [s for s in message if isinstance(s, dict)]
        if isinstance(message, str) and message:
            return [{"type": "text", "data": {"text": message}}]
        return []

    def _parse_segments(self, segments):
        text, images, mentioned, reply = [], [], False, None
        for segment in segments:
            data = segment.get("data") or {}
            if not isinstance(data, dict):
                continue
            kind = segment.get("type")
            if kind == "text":
                text.append(str(data.get("text") or ""))
            elif kind == "at":
                qq = str(data.get("qq") or "")
                if qq == str(self._self_id):
                    mentioned = True
                elif qq:
                    text.append("@" + qq)
            elif kind == "reply":
                try:
                    reply = int(data["id"])
                except (KeyError, ValueError, TypeError):
                    pass
            elif kind == "image":
                images.append(data)
            elif kind == "face":
                text.append(f"[face:{data.get('id', '')}]")
        return " ".join(t.strip() for t in text if t.strip()), images, mentioned, reply

    async def send(self, message: OutboundMessage):
        kind, _, target = message.chat_id.partition(":")
        if kind not in {"group", "private"} or not target.isdecimal():
            raise ValueError("Invalid QQ outbound target")
        if kind == "group":
            self._assert_group_output_allowed(target)
        elif not self.config.private_replies_enabled:
            return
        segments = []
        for ref in message.media:
            source = str(ref)
            parsed = urlsplit(source)
            if parsed.scheme in {"https", "http"}:
                file = await asyncio.to_thread(_public_image_url, source)
            else:
                file = await asyncio.to_thread(_local_image, source, self.config.max_image_bytes)
            segments.append({"type": "image", "data": {"file": file}})
        if message.content.strip():
            segments.append({"type": "text", "data": {"text": message.content.strip()}})
        if not segments:
            return
        params = {
            "message": segments,
            "message_type": kind,
            "group_id" if kind == "group" else "user_id": int(target),
        }
        result = await self._call_action("send_msg", params)
        self.sent_count += 1
        data = result.get("data") or {}
        if isinstance(data, dict) and data.get("message_id") is not None:
            self._bot_outbound_ids.append(int(data["message_id"]))

    async def _on_notice(self, event):
        if event.get("notice_type") != "group_increase" or not self.config.welcome_new_members:
            return
        if self._automatic_quiet_active():
            return
        group = str(event.get("group_id") or "")
        member = str(event.get("user_id") or "")
        if group.isdecimal() and member.isdecimal():
            self._assert_group_output_allowed(group)
            await self._reply("group", group, f"欢迎 @{member} 加入！发送 #帮助 查看群功能。")

    async def _on_message(self, event):
        # No unmatched event is forwarded to another framework or another agent.
        return

    async def stop(self):
        self._running = False
        tasks = list(self._background_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._background_tasks.clear()


class AstrBotCSApplication(PWCS2Application, AstrBotOneBotTransport):
    async def _handle_ai_message(self, *args, **kwargs):
        # Interactive chat belongs to AstrBot and its real Dafeiyu plugins.
        # The shared AI service remains only for existing scheduled reports/cards.
        return


def create_application(settings: dict[str, Any]) -> AstrBotCSApplication:
    parsed = PWCS2Config.model_validate(settings)
    if parsed.forward_unmatched:
        raise ValueError("AstrBot runtime must not forward to the legacy gateway")
    return AstrBotCSApplication(parsed)
