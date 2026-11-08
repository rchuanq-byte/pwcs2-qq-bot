"""Small version-pinned AstrBot integration helpers; no legacy gateway imports."""

from __future__ import annotations

import asyncio
import copy
import json
import os
import re
import sys
from contextlib import asynccontextmanager
from pathlib import Path

# AstrBot's built-in optional group reply runs at maxsize - 2. Our ACL and
# replay/media checks must finish before any handler can yield a ProviderRequest.
NIUNIU_ROUTE_PRIORITY = sys.maxsize


def defer_framework_auto_reply(event, *, omit=False):
    """Keep passive collectors ahead of the optional built-in random reply.

    AstrBot 4.29 iterates this exact list while running the first (Niuniu) route.
    Mutate it in place, preserving every other handler's relative order, so a
    claimed proactive turn cannot race a second built-in reply to the same input.
    """
    handlers = event.get_extra("activated_handlers")
    if not isinstance(handlers, list):
        return
    deferred = [
        handler
        for handler in handlers
        if getattr(handler, "handler_module_path", "") == "astrbot.builtin_stars.astrbot.main"
        and getattr(handler, "handler_name", "") == "on_message"
    ]
    if deferred:
        handlers[:] = [h for h in handlers if not any(h is d for d in deferred)] + (
            [] if omit else deferred
        )


def automatic_turn_claimed(event):
    """A real response or the pinned Dafeiyu turn contract owns this input."""
    claim = event.get_extra("dsh.link.v1.turn_claim")
    return bool(
        getattr(event, "_has_send_oper", False)
        or (
            isinstance(claim, dict)
            and claim.get("version") == 1
            and (claim.get("block_proactive") or claim.get("already_replied"))
        )
    )


def configure_conversation_frequency(environment, *, upstream_defaults: bool):
    """Remove deployment pacing overrides so the pinned plugins use their defaults."""
    local_overrides = {
        "DSH_PROACTIVE_MIN_INTERVAL_MS": "1800000",
        "DSH_PROACTIVE_DAY_MAX": "8",
        "DSH_INITIATE_IDLE": "5400",
        "DSH_INITIATE_COOLDOWN": "10800",
        "DSH_INITIATE_DAY_MAX": "2",
    }
    for name, value in local_overrides.items():
        if upstream_defaults:
            environment.pop(name, None)
        else:
            environment[name] = value


def load_business_settings(path: Path) -> dict:
    def expand(value):
        if isinstance(value, dict):
            return {key: expand(item) for key, item in value.items()}
        if isinstance(value, list):
            return [expand(item) for item in value]
        if isinstance(value, str):
            return re.sub(r"\$\{([A-Z0-9_]+)\}", lambda m: os.environ.get(m[1], ""), value)
        return value

    with path.open(encoding="utf-8-sig") as stream:
        document = expand(json.load(stream))
    settings = document.get("channels", {}).get("pwcs2", document)
    return {**settings, "forwardUnmatched": False}


def turn_tail(messages: list[dict]) -> list[dict]:
    """Keep a whole current user/tool/assistant turn, never a stale history prefix."""
    start = next(
        (i for i in range(len(messages) - 1, -1, -1) if messages[i].get("role") == "user"), None
    )
    return copy.deepcopy(messages[start:]) if start is not None else []


def bounded_history(messages: list[dict], maximum=48) -> list[dict]:
    if len(messages) <= maximum:
        return messages
    start = len(messages) - maximum
    while start < len(messages) and messages[start].get("role") != "user":
        start += 1
    return messages[start:]


def clean_completed_turn(messages, original_user):
    """Do not persist base64 pictures, injected profiles, reasoning or tool dumps."""
    tail = turn_tail(messages)
    answers = [m for m in tail if m.get("role") == "assistant" and not m.get("tool_calls")]
    if not tail or not answers:
        return []
    content = answers[-1].get("content") or ""
    if isinstance(content, list):
        content = "\n".join(
            p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text"
        )
    if not isinstance(content, str) or not content.strip():
        return []
    user = original_user if original_user is not None else tail[0].get("content", "")
    if not isinstance(user, str):
        return []
    return [{"role": "user", "content": user}, {"role": "assistant", "content": content}]


class SharedGroupConcurrency:
    """20 model turns may run concurrently; only the shared-history append is serialized.

    AstrBot 4.29 normally locks an entire group for the duration of an LLM call.
    This adapter hook is scoped to our plugin's AI events. Core sources stay unmodified.
    Each completion appends only its own turn to the latest database snapshot, avoiding
    the lost-update race from replacing shared history with an old request snapshot.
    """

    def __init__(self, current_event, maximum=20):
        self.current_event = current_event
        self.slots = asyncio.Semaphore(maximum)
        self.history_lock = asyncio.Lock()
        self._original_lock = None
        self._original_update = None

    def install(self, lock_manager, manager):
        original_lock = lock_manager.acquire_lock
        original_update = manager.update_conversation
        self._original_lock, self._original_update = original_lock, original_update

        @asynccontextmanager
        async def lock(umo):
            event = self.current_event()
            if event is not None and event.get_extra("niuniu_ai"):
                async with self.slots:
                    yield
            else:
                async with original_lock(umo):
                    yield

        async def update(umo, conversation_id=None, history=None, **kwargs):
            event = self.current_event()
            if (
                event is None
                or not event.get_extra("niuniu_ai")
                or history is None
                or not event.get_extra("niuniu_request_started")
            ):
                return await original_update(umo, conversation_id, history=history, **kwargs)
            # Suppressed, failed or superseded automatic turns must not become things
            # the bot supposedly said. Explicit completed turns retain the full answer.
            if event.get_extra("niuniu_suppress") or event.get_extra("niuniu_history_saved"):
                return
            tail = clean_completed_turn(history, event.get_extra("niuniu_history_user"))
            if not tail or not any(m.get("role") == "assistant" for m in tail):
                return
            if not event.get_extra("niuniu_explicit"):
                # Automatic drafts are not conversation facts until actually sent.
                event.set_extra("niuniu_pending_history", (umo, conversation_id, tail, kwargs))
                return
            await self._commit(event, umo, conversation_id, tail, kwargs)

        lock_manager.acquire_lock = lock
        manager.update_conversation = update
        self._lock_manager, self._manager = lock_manager, manager
        self._lock, self._update = lock, update

    async def _commit(self, event, umo, conversation_id, tail, kwargs):
        if event.get_extra("niuniu_suppress") or event.get_extra("niuniu_history_saved"):
            return
        async with self.history_lock:
            cid = await self._manager.get_curr_conversation_id(umo)
            if cid != conversation_id:
                return  # Clear/new invalidates older in-flight answers.
            current = await self._manager.get_conversation(umo, cid)
            previous = json.loads(current.history) if current and current.history else []
            await self._original_update(
                umo, cid, history=bounded_history(previous + tail), **kwargs
            )
            event.set_extra("niuniu_history_saved", True)

    async def sent(self, event):
        pending = event.get_extra("niuniu_pending_history")
        if pending:
            event.set_extra("niuniu_pending_history", None)
            await self._commit(event, *pending)

    def uninstall(self):
        if self._original_lock is None:
            return
        if self._lock_manager.acquire_lock is self._lock:
            self._lock_manager.acquire_lock = self._original_lock
        if self._manager.update_conversation is self._update:
            self._manager.update_conversation = self._original_update
