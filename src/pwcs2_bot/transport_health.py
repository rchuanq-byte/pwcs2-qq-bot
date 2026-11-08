"""Small, credential-free health snapshots for the *active* OneBot connection."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from loguru import logger


def write_snapshot(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=".napcat-health-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(state, stream, separators=(",", ":"))
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


class TransportHealthMonitor:
    """Probe through the bot socket, not a separate socket that could hide a hang.

    Only reconnect the bot's own stale socket. Never restart QQ, send a group
    message, or try to bypass a login challenge. All persisted errors are enums.
    """

    def __init__(
        self,
        *,
        path: Path,
        connection: Callable[[], Any],
        probe: Callable[[], Awaitable[dict[str, Any]]],
        interval: float = 30,
        timeout: float = 5,
        failure_threshold: int = 2,
    ) -> None:
        self.path = path
        self.connection = connection
        self.probe = probe
        self.interval = interval
        self.timeout = timeout
        self.failure_threshold = failure_threshold
        self._failed_connection: Any = None
        self._failures = 0
        self._last_ok = 0.0
        self._last_state = ""
        self._write_warning = False

    async def publish(self, state: str, *, connected: bool, online: bool = False) -> None:
        now = time.time()
        if state == "online":
            self._last_ok = now
        payload = {
            "version": 1,
            "checked_at": now,
            "last_ok_at": self._last_ok,
            "state": state,
            "connected": connected,
            "online": online,
            "consecutive_failures": self._failures,
        }
        try:
            await asyncio.to_thread(write_snapshot, self.path, payload)
            self._write_warning = False
        except OSError:
            if not self._write_warning:
                logger.warning("pwcs2: cannot persist QQ health snapshot")
                self._write_warning = True
        if state != self._last_state:
            logger.info("pwcs2: QQ transport health={}", state)
            self._last_state = state

    async def check_once(self) -> str:
        socket = self.connection()
        if socket is None:
            self._failed_connection = None
            self._failures = 0
            await self.publish("disconnected", connected=False)
            return "disconnected"
        if socket is not self._failed_connection:
            self._failed_connection = socket
            self._failures = 0
        try:
            async with asyncio.timeout(self.timeout):
                result = await self.probe()
            data = result.get("data")
            if (
                result.get("status") != "ok"
                or result.get("retcode") != 0
                or not isinstance(data, dict)
                or not isinstance(data.get("online"), bool)
                or not isinstance(data.get("good"), bool)
            ):
                raise ValueError("invalid OneBot status")
        except Exception:
            # Do not leak an upstream exception (which may contain tokens).
            if self.connection() is not socket:
                self._failures = 0
                await self.publish("reconnecting", connected=False)
                return "reconnecting"
            self._failures += 1
            state = "unresponsive"
            if self._failures >= self.failure_threshold:
                state = "reconnecting"
                try:
                    async with asyncio.timeout(self.timeout):
                        await socket.close(code=1011, reason="OneBot health probe failed")
                except Exception:
                    # websockets may still be waiting for its close handshake.
                    # Abort this socket only, and only if it is still ours.
                    if self.connection() is socket:
                        transport = getattr(socket, "transport", None)
                        if transport is not None:
                            transport.abort()
            await self.publish(state, connected=False)
            return state
        self._failures = 0
        if self.connection() is not socket:
            await self.publish("reconnecting", connected=False)
            return "reconnecting"
        if not data["online"]:
            state = "login_required"
        elif not data["good"]:
            state = "degraded"
        else:
            state = "online"
        await self.publish(state, connected=True, online=data["online"])
        return state

    async def run(self) -> None:
        await self.publish("starting", connected=False)
        try:
            # Allow the initial get_login_info handshake to complete first.
            while True:
                await asyncio.sleep(self.interval)
                await self.check_once()
        finally:
            await self.publish("stopped", connected=False)
