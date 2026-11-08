"""Poll configured live-stream targets and announce confirmed live edges."""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from dataclasses import asdict, replace

from loguru import logger

from pwcs2_bot.database import BindingRepository, StreamLiveNotification
from pwcs2_bot.stream_live import (
    LivePlatformClient,
    LiveSnapshot,
    StreamerTarget,
    StreamLiveError,
    format_live_notification,
)

SendLive = Callable[[str, str], Awaitable[None]]


class StreamLiveMonitor:
    def __init__(
        self,
        repository: BindingRepository,
        client: LivePlatformClient,
        send_live: SendLive,
        *,
        targets: list[StreamerTarget] | tuple[StreamerTarget, ...],
        allowed_groups: list[str] | tuple[str, ...],
        enabled: bool = True,
        poll_interval_seconds: float = 60,
        initial_delay_seconds: float = 20,
        confirmation_polls: int = 2,
        jitter_ratio: float = 0.15,
        delivery_base_delay_seconds: int = 60,
        delivery_max_delay_seconds: int = 900,
        delivery_max_attempts: int = 5,
    ) -> None:
        self.repository = repository
        self.client = client
        self.send_live = send_live
        self.targets = tuple(target for target in targets if target.enabled)
        self.allowed_groups = tuple(
            dict.fromkeys(
                group_id
                for item in allowed_groups
                if (group_id := str(item).strip())
            )
        )
        self.enabled = enabled and bool(self.targets) and bool(self.allowed_groups)
        self.poll_interval_seconds = float(poll_interval_seconds)
        self.initial_delay_seconds = float(initial_delay_seconds)
        self.confirmation_polls = int(confirmation_polls)
        self.jitter_ratio = max(0.0, min(float(jitter_ratio), 0.5))
        self.delivery_base_delay_seconds = max(0, int(delivery_base_delay_seconds))
        self.delivery_max_delay_seconds = max(0, int(delivery_max_delay_seconds))
        self.delivery_max_attempts = max(1, int(delivery_max_attempts))
        self._stop_event = asyncio.Event()
        self._failure_counts: dict[tuple[str, str], int] = {}

    def stop(self) -> None:
        self._stop_event.set()

    async def close(self) -> None:
        await self.client.close()

    async def run(self) -> None:
        if not self.enabled:
            if not self.allowed_groups:
                logger.warning("pwcs2: live monitor disabled because allowedGroups is empty")
            return
        if await self._wait(self.initial_delay_seconds):
            return
        logger.info(
            "pwcs2: live monitor started targets={} groups={} interval={}s",
            len(self.targets),
            len(self.allowed_groups),
            self.poll_interval_seconds,
        )
        while not self._stop_event.is_set():
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("pwcs2: unexpected live monitor failure")
            jitter = self.poll_interval_seconds * self.jitter_ratio
            delay = max(1.0, self.poll_interval_seconds + random.uniform(-jitter, jitter))
            if await self._wait(delay):
                return

    async def _wait(self, seconds: float) -> bool:
        if seconds <= 0:
            return self._stop_event.is_set()
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=seconds)
        except TimeoutError:
            return False
        return True

    async def poll_once(self) -> tuple[LiveSnapshot, ...]:
        results = await asyncio.gather(
            *(self.client.fetch(target) for target in self.targets),
            return_exceptions=True,
        )
        snapshots: list[LiveSnapshot] = []
        for target, result in zip(self.targets, results, strict=True):
            key = (target.platform, target.room_id)
            if isinstance(result, BaseException):
                if isinstance(result, asyncio.CancelledError):
                    raise result
                failures = self._failure_counts.get(key, 0) + 1
                self._failure_counts[key] = failures
                if failures == 1 or failures % 10 == 0:
                    logger.warning(
                        "pwcs2: live status fetch failed platform={} room={} failures={}: {}",
                        target.platform,
                        target.room_id,
                        failures,
                        result,
                    )
                continue
            if not isinstance(result, LiveSnapshot):
                raise StreamLiveError("live provider returned an invalid snapshot")
            if self._failure_counts.pop(key, 0):
                logger.info(
                    "pwcs2: live status fetch recovered platform={} room={}",
                    target.platform,
                    target.room_id,
                )
            if result.is_replay:
                result = replace(
                    result,
                    is_live=False,
                    session_key="",
                    started_at="",
                )
            snapshots.append(result)
            self.repository.record_stream_live_observation(
                result.platform,
                result.room_id,
                is_live=result.is_live,
                session_key=result.session_key,
                snapshot=asdict(result),
                group_ids=self.allowed_groups,
                confirmation_polls=self.confirmation_polls,
            )

        await self._deliver_pending()
        return tuple(snapshots)

    async def _deliver_pending(self) -> None:
        for notification in self.repository.claim_stream_live_notifications():
            try:
                snapshot = LiveSnapshot.from_payload(notification.snapshot)
                if snapshot.is_replay:
                    logger.info(
                        "pwcs2: suppressed replay notification platform={} room={} group={}",
                        notification.platform,
                        notification.room_id,
                        notification.group_id,
                    )
                    self.repository.mark_stream_live_notification_sent(notification)
                    continue
                await self.send_live(
                    notification.group_id,
                    format_live_notification(snapshot),
                )
            except asyncio.CancelledError:
                self.repository.retry_stream_live_notification(
                    notification,
                    "delivery cancelled",
                    base_delay_seconds=self.delivery_base_delay_seconds,
                    max_delay_seconds=self.delivery_max_delay_seconds,
                    max_attempts=self.delivery_max_attempts,
                )
                raise
            except Exception as exc:
                retrying = self.repository.retry_stream_live_notification(
                    notification,
                    f"{type(exc).__name__}: {exc}",
                    base_delay_seconds=self.delivery_base_delay_seconds,
                    max_delay_seconds=self.delivery_max_delay_seconds,
                    max_attempts=self.delivery_max_attempts,
                )
                logger.warning(
                    "pwcs2: live notification delivery failed group={} "
                    "platform={} room={} attempt={} retrying={}: {}",
                    notification.group_id,
                    notification.platform,
                    notification.room_id,
                    notification.attempt_count,
                    retrying,
                    exc,
                )
                continue
            self.repository.mark_stream_live_notification_sent(notification)


def notification_snapshot(notification: StreamLiveNotification) -> LiveSnapshot:
    """Compatibility helper kept small for diagnostics and tests."""
    return LiveSnapshot.from_payload(notification.snapshot)
