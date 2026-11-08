"""Isolated Demo acquisition and analysis controller.

The QQ process only writes small durable intents.  This service performs the
network download and native Demo parsing in its own resource-limited container,
then creates a lease-based job for a Windows CS2 renderer.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import os
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from loguru import logger

from pwcs2_bot.database import BindingRepository, HighlightRenderJob
from pwcs2_bot.demo_highlights import (
    DemoHighlightAnalyzer,
    DemoParseError,
    Demoparser2Adapter,
    HighlightWindow,
)
from pwcs2_bot.demo_source import DemoRequest, DemoSourceError, DemoSourceManager
from pwcs2_bot.highlight_jobs import HighlightJobStore
from pwcs2_bot.highlight_protocol import (
    HighlightProtocolError,
    HighlightSegment,
    normalize_cs2_patch,
)

_PWA_MAX_DOWNLOAD_BYTES = 600 * 1024 * 1024
_STALE_SOURCE_STAGING_SECONDS = 6 * 60 * 60
_MANAGED_SOURCE_DEMO_NAME = re.compile(r"[0-9a-f]{16}-[0-9a-f]{16}\.dem\Z")
_MANAGED_SOURCE_PLATFORM = re.compile(r"[a-z][a-z0-9_-]{0,31}\Z")


def _normalize_cs2_patch_version(value: object) -> str:
    """Normalize ``1.41.8.0`` and ``14180`` to the same compact form."""

    return normalize_cs2_patch(value)


def _read_demo_patch_version(path: Path) -> str:
    """Read only a Demo header so stale files never reach the Windows worker."""

    try:
        from demoparser2 import DemoParser  # type: ignore[import-not-found]
    except ImportError as exc:
        raise DemoParseError("demoparser2 is not installed in the controller") from exc
    try:
        header = dict(DemoParser(str(path)).parse_header() or {})
    except Exception as exc:
        raise DemoParseError(f"unable to parse Demo header: {exc}") from exc
    return _normalize_cs2_patch_version(header.get("patch_version"))


class _HighlightIntentLeaseLost(RuntimeError):
    """The durable controller intent is no longer owned by this process."""


class _HighlightIntentLeaseHeartbeat:
    """Keep one controller intent live while acquisition and parsing run.

    ``claimed_at`` is the durable lease clock.  Demo downloads and native parsing
    can both outlive the initial lease, so merely checking the token when the
    final result is committed is too late: another controller may already have
    repeated all of the expensive work.  This helper refreshes the clock in the
    background and exposes an explicit CAS guard for irreversible side effects.
    """

    def __init__(
        self,
        repository: BindingRepository,
        job: HighlightRenderJob,
        *,
        interval_seconds: float,
    ) -> None:
        self._repository = repository
        self._job = job
        self._interval_seconds = max(0.01, float(interval_seconds))
        self._stop_event = asyncio.Event()
        self._renew_lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None
        self._failure: _HighlightIntentLeaseLost | None = None

    async def __aenter__(self) -> _HighlightIntentLeaseHeartbeat:
        if self._task is not None:
            raise RuntimeError("highlight intent lease heartbeat is already running")
        self._stop_event.clear()
        self._task = asyncio.create_task(
            self._run(),
            name=f"highlight-intent-lease-{self._job.job_id}",
        )
        return self

    async def __aexit__(self, exc_type, _exc, _traceback) -> bool:
        await self.stop()
        # Cancellation must retain its normal semantics.  For every other exit,
        # a detected lease loss takes precedence so the stale owner cannot retry
        # or complete the intent using its old token.
        if exc_type is not asyncio.CancelledError:
            self.raise_if_lost()
        return False

    async def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=self._interval_seconds,
                )
            except TimeoutError:
                pass
            if self._stop_event.is_set():
                return
            try:
                await self.ensure_owned(renew_now=True)
            except _HighlightIntentLeaseLost:
                return

    async def ensure_owned(self, *, renew_now: bool = False) -> None:
        """Raise after lease loss; optionally perform a fresh durable CAS."""
        self.raise_if_lost()
        if not renew_now:
            return
        async with self._renew_lock:
            self.raise_if_lost()
            try:
                renewed = await asyncio.to_thread(
                    self._repository.renew_highlight_render_job_lease,
                    self._job,
                )
            except Exception:
                logger.exception(
                    "pwcs2: Demo highlight intent heartbeat failed group={} match={}",
                    self._job.group_id,
                    self._job.match_id,
                )
                self._mark_lost("highlight intent lease heartbeat failed")
            else:
                if not renewed:
                    self._mark_lost("highlight intent lease ownership changed")
            self.raise_if_lost()

    def _mark_lost(self, message: str) -> None:
        if self._failure is None:
            self._failure = _HighlightIntentLeaseLost(message)
            logger.warning(
                "pwcs2: Demo highlight intent lease lost group={} match={}",
                self._job.group_id,
                self._job.match_id,
            )

    def raise_if_lost(self) -> None:
        if self._failure is not None:
            raise self._failure

    async def stop(self) -> None:
        task = self._task
        if task is None:
            return
        self._stop_event.set()
        try:
            await task
        finally:
            self._task = None


class HighlightController:
    """Bridge durable match intents to authenticated Windows render jobs."""

    def __init__(
        self,
        repository: BindingRepository,
        demo_sources: DemoSourceManager,
        worker_jobs: HighlightJobStore,
        *,
        parser: Demoparser2Adapter | None = None,
        analyzer: DemoHighlightAnalyzer | None = None,
        worker_id: str = "server-demo-controller",
        poll_interval_seconds: float = 30,
        initial_delay_seconds: float = 15,
        max_backlog_demos: int = 2,
        max_backlog_demo_bytes: int = 2 * 1024 * 1024 * 1024,
        acquisition_reservation_bytes: int | None = None,
        intent_lease_seconds: int = 1800,
        intent_lease_renew_interval_seconds: float = 300,
        renderer_patch_version: str = "",
        demo_patch_reader: Callable[[Path], str] | None = None,
    ) -> None:
        self.repository = repository
        self.demo_sources = demo_sources
        self.worker_jobs = worker_jobs
        self.parser = parser or Demoparser2Adapter()
        self.analyzer = analyzer or DemoHighlightAnalyzer()
        self.worker_id = worker_id
        self.poll_interval_seconds = max(5.0, float(poll_interval_seconds))
        self.initial_delay_seconds = max(0.0, float(initial_delay_seconds))
        self.max_backlog_demos = max(1, int(max_backlog_demos))
        self.max_backlog_demo_bytes = max(1, int(max_backlog_demo_bytes))
        # During acquisition the compressed response, unpacked Demo, and
        # manager-owned copy can coexist.  Reserve that peak before claiming a
        # durable intent so a crash/retry cannot overrun the disk budget.
        default_reservation = _PWA_MAX_DOWNLOAD_BYTES + 2 * self.demo_sources.max_file_bytes
        self.acquisition_reservation_bytes = max(
            1,
            int(
                default_reservation
                if acquisition_reservation_bytes is None
                else acquisition_reservation_bytes
            ),
        )
        self.intent_lease_seconds = max(1, int(intent_lease_seconds))
        self.intent_lease_renew_interval_seconds = max(
            0.01,
            min(
                float(intent_lease_renew_interval_seconds),
                self.intent_lease_seconds / 3,
            ),
        )
        # The Windows renderer cannot replay a Demo built by another CS2
        # version. Keeping this value explicit makes an upstream CS2 update a
        # safe server-side skip rather than a queue full of doomed jobs.
        self.renderer_patch_version = _normalize_cs2_patch_version(renderer_patch_version)
        self.demo_patch_reader = demo_patch_reader or _read_demo_patch_version
        self._backpressure_active = False
        self._stop_event = asyncio.Event()

    async def run(self) -> None:
        self._stop_event.clear()
        if self.initial_delay_seconds and await self._wait(self.initial_delay_seconds):
            return
        logger.info("pwcs2: Demo highlight controller started")
        while not self._stop_event.is_set():
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("pwcs2: unexpected Demo highlight controller failure")
            if await self._wait(self.poll_interval_seconds):
                return

    def stop(self) -> None:
        self._stop_event.set()

    async def _wait(self, seconds: float) -> bool:
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=seconds)
        except TimeoutError:
            return False
        return True

    async def run_once(self) -> int:
        stale_staging = await asyncio.to_thread(
            self.demo_sources.cleanup_stale_staging,
            older_than_seconds=_STALE_SOURCE_STAGING_SECONDS,
        )
        if stale_staging:
            logger.warning(
                "pwcs2: removed {} abandoned Demo staging director{}",
                len(stale_staging),
                "y" if len(stale_staging) == 1 else "ies",
            )
        source_usage = await asyncio.to_thread(self.demo_sources.storage_usage)
        backlog = await asyncio.to_thread(self.worker_jobs.demo_backlog)
        combined_demo_count = backlog.demo_count + source_usage.demo_count
        combined_bytes = backlog.demo_bytes + source_usage.total_bytes
        has_capacity = (
            combined_demo_count < self.max_backlog_demos
            and combined_bytes + self.acquisition_reservation_bytes
            <= self.max_backlog_demo_bytes
        )
        if not has_capacity:
            if not self._backpressure_active:
                logger.warning(
                    "pwcs2: Demo highlight acquisition paused by backlog "
                    "demos={}/{} bytes={}/{} reserved={} source_bytes={}",
                    combined_demo_count,
                    self.max_backlog_demos,
                    combined_bytes,
                    self.max_backlog_demo_bytes,
                    self.acquisition_reservation_bytes,
                    source_usage.total_bytes,
                )
                self._backpressure_active = True
            return 0
        if self._backpressure_active:
            logger.info(
                "pwcs2: Demo highlight acquisition resumed demos={} bytes={} source_bytes={}",
                combined_demo_count,
                combined_bytes,
                source_usage.total_bytes,
            )
            self._backpressure_active = False
        jobs = await asyncio.to_thread(
            self.repository.claim_highlight_render_jobs,
            self.worker_id,
            limit=1,
            lease_seconds=self.intent_lease_seconds,
        )
        if not jobs:
            return 0
        job = jobs[0]
        heartbeat = _HighlightIntentLeaseHeartbeat(
            self.repository,
            job,
            interval_seconds=self.intent_lease_renew_interval_seconds,
        )
        try:
            async with heartbeat:
                output = await self._process(job, heartbeat)
                # Also protect the no-highlight result and the final intent
                # transition with a fresh durable ownership check.
                await heartbeat.ensure_owned(renew_now=True)
        except asyncio.CancelledError:
            raise
        except _HighlightIntentLeaseLost:
            # A new controller owns the intent (or durable lease state could not
            # be verified).  The old owner must not enqueue, retry, or complete.
            return 1
        except DemoSourceError as exc:
            await asyncio.to_thread(
                self.repository.retry_highlight_render_job,
                job,
                str(exc),
                base_delay_seconds=300,
                max_delay_seconds=3600,
                max_attempts=12,
            )
            return 1
        except (DemoParseError, HighlightProtocolError, ValueError) as exc:
            await asyncio.to_thread(
                self.repository.retry_highlight_render_job,
                job,
                str(exc),
                base_delay_seconds=120,
                max_delay_seconds=1800,
                max_attempts=3,
            )
            return 1
        except Exception:
            logger.exception(
                "pwcs2: Demo highlight intent failed group={} match={}",
                job.group_id,
                job.match_id,
            )
            await asyncio.to_thread(
                self.repository.retry_highlight_render_job,
                job,
                "unexpected Demo highlight controller failure",
                base_delay_seconds=120,
                max_delay_seconds=1800,
                max_attempts=3,
            )
            return 1
        completed = await asyncio.to_thread(
            self.repository.complete_highlight_render_job,
            job,
            output,
        )
        if not completed:
            logger.warning(
                "pwcs2: Demo highlight intent lease changed group={} match={}",
                job.group_id,
                job.match_id,
            )
        return 1

    async def _process(
        self,
        job: HighlightRenderJob,
        heartbeat: _HighlightIntentLeaseHeartbeat,
    ) -> dict[str, Any]:
        payload = _validate_intent(job)
        artifact = await self.demo_sources.acquire(
            DemoRequest(
                match_id=job.match_id,
                steam_id=job.steam_id,
                platform=str(payload["source"]),
            ),
            provider_name="pwa",
        )
        if artifact is None:
            raise DemoSourceError("PWA Demo is not available yet")
        try:
            await heartbeat.ensure_owned()
            renderer_patches = await asyncio.to_thread(
                self.worker_jobs.renderer_patches, fallback=self.renderer_patch_version
            )
            observed_patch = ""
            if renderer_patches:
                expected_patch = renderer_patches[0]
                observed_patch = _normalize_cs2_patch_version(
                    await asyncio.to_thread(self.demo_patch_reader, artifact.path)
                )
                if observed_patch and int(observed_patch) < int(expected_patch):
                    logger.info(
                        "pwcs2: skipped Demo incompatible with Windows renderer "
                        "group={} match={} demo_patch={} renderer_patch={}",
                        job.group_id,
                        job.match_id,
                        observed_patch,
                        expected_patch,
                    )
                    return {
                        "status": "skipped_renderer_version",
                        "demo_patch": observed_patch,
                        "renderer_patch": expected_patch,
                    }

            def parse_and_select() -> tuple[HighlightWindow, ...]:
                timeline = self.parser.parse(artifact.path)
                if timeline.match_id != job.match_id:
                    from dataclasses import replace

                    timeline = replace(timeline, match_id=job.match_id)
                return self.analyzer.analyze(
                    timeline,
                    tracked_steam_ids=payload["tracked_steam_ids"],
                )

            windows = await asyncio.to_thread(parse_and_select)
            await heartbeat.ensure_owned()
            if not windows:
                return {
                    "status": "no_highlight",
                    "demo_sha256": artifact.sha256,
                }
            # A qualifying lowlight is deliberately preferred over an ordinary
            # kill highlight.  The analyzer emits at most one per match, so a
            # funny failure can actually reach the renderer without doubling
            # Demo storage or group-message volume.
            best = next((item for item in windows if _is_lowlight(item)), windows[0])
            segment = HighlightSegment(
                start_tick=best.start_tick,
                end_tick=best.end_tick,
                label=_highlight_label(best),
                score=float(best.score),
            )
            identity = f"{job.group_id}\n{best.highlight_id}".encode()
            worker_job_id = "h" + hashlib.sha256(identity).hexdigest()[:31]
            # A fresh CAS immediately before copying the Demo into the worker
            # queue prevents a stale controller from producing a second job.
            await heartbeat.ensure_owned(renew_now=True)
            await asyncio.to_thread(
                self.worker_jobs.enqueue_demo,
                group_id=job.group_id,
                match_id=job.match_id,
                steam_id=best.player_steam_id,
                demo_path=artifact.path,
                segments=(segment,),
                context={
                    "schema_version": 1,
                    "source": payload["source"],
                    "highlight": best.to_payload(),
                    "match": payload.get("match", {}),
                },
                job_id=worker_job_id,
                demo_patch=observed_patch,
            )
            logger.info(
                "pwcs2: Demo highlight prepared group={} match={} player={} score={}",
                job.group_id,
                job.match_id,
                best.player_steam_id,
                best.score,
            )
            return {
                "status": "queued_for_windows",
                "worker_job_id": worker_job_id,
                "highlight_id": best.highlight_id,
                "score": best.score,
            }
        finally:
            await asyncio.to_thread(
                _remove_managed_demo,
                artifact.path,
                self.demo_sources.storage_path,
            )


def _validate_intent(job: HighlightRenderJob) -> dict[str, Any]:
    payload = job.payload
    if payload.get("schema_version") != 1 or payload.get("kind") != "demo_match":
        raise ValueError("unsupported Demo highlight intent")
    source = str(payload.get("source") or "").strip().casefold()
    if source != "perfectworld":
        raise ValueError("automatic Demo acquisition currently supports Perfect World only")
    raw_ids = payload.get("tracked_steam_ids")
    if not isinstance(raw_ids, list):
        raise ValueError("Demo highlight intent is missing tracked players")
    tracked = tuple(
        dict.fromkeys(
            str(value).strip()
            for value in raw_ids
            if str(value).strip().isdecimal() and len(str(value).strip()) == 17
        )
    )
    if not tracked:
        raise ValueError("Demo highlight intent has no valid tracked players")
    result = dict(payload)
    result["source"] = source
    result["tracked_steam_ids"] = tracked
    if not isinstance(result.get("match"), dict):
        result["match"] = {}
    return result


def _highlight_label(window: HighlightWindow) -> str:
    names = {
        "ace": "ACE",
        "4k": "四杀",
        "3k": "三杀",
        "headshot_streak": "连续爆头",
        "knife_kill": "刀杀",
        "clutch_clue": "残局",
        "lowlight_close_whiff": "近战描边",
        "lowlight_self_flash": "自闪暴毙",
        "lowlight_self_damage": "自残暴毙",
        "lowlight_knife_fail": "刀人失败",
        "lowlight_advantage_throw": "优势残局送掉",
        "lowlight_teamkill": "痛击队友",
        "lowlight_world_death": "离谱暴毙",
    }
    rendered = [names[tag] for tag in window.tags if tag in names]
    prefix = "下饭时刻 · " if _is_lowlight(window) else ""
    return (prefix + " · ".join(rendered))[:80] or "Demo 高光"


def _is_lowlight(window: HighlightWindow) -> bool:
    return any(tag.startswith("lowlight_") for tag in window.tags)


def _remove_managed_demo(path: Path, storage_root: Path) -> None:
    """Delete the acquisition copy after the worker store made its own copy."""
    try:
        if path.is_symlink() or path.parent.is_symlink() or storage_root.is_symlink():
            raise ValueError
        root = storage_root.resolve(strict=True)
        resolved = path.resolve(strict=True)
        if (
            resolved.parent.parent != root
            or resolved.parent != path.parent.resolve(strict=True)
            or _MANAGED_SOURCE_PLATFORM.fullmatch(resolved.parent.name) is None
            or _MANAGED_SOURCE_DEMO_NAME.fullmatch(resolved.name) is None
            or not resolved.is_file()
        ):
            raise ValueError
        resolved.unlink()
    except FileNotFoundError:
        return
    except (OSError, ValueError):
        logger.warning("pwcs2: could not safely remove a managed Demo artifact")


async def _main_async(args: argparse.Namespace) -> None:
    from pwcs2_bot.pwa_demo_adapter import create_pwa_demo_provider_from_env

    provider = create_pwa_demo_provider_from_env()
    if provider is None:
        raise SystemExit("PWCS2_PWA_DEMO_ENABLED is disabled")
    controller = HighlightController(
        BindingRepository(args.database),
        DemoSourceManager(
            args.demo_storage,
            args.demo_import_root,
            max_file_bytes=args.max_demo_bytes,
            providers=(provider,),
            shared_root_gid=args.shared_data_gid,
        ),
        HighlightJobStore(
            args.highlight_data,
            max_demo_bytes=args.max_demo_bytes,
            shared_root_gid=args.shared_data_gid,
            shared_output_gid=args.shared_data_gid,
        ),
        poll_interval_seconds=args.poll_interval,
        initial_delay_seconds=args.initial_delay,
        max_backlog_demos=args.max_backlog_demos,
        max_backlog_demo_bytes=args.max_backlog_demo_bytes,
        renderer_patch_version=os.environ.get("PWCS2_HIGHLIGHT_RENDERER_PATCH", ""),
    )
    try:
        await controller.run()
    finally:
        controller.stop()


def main() -> None:
    parser = argparse.ArgumentParser(description="CS2 Demo highlight controller")
    parser.add_argument(
        "--database",
        default=os.environ.get(
            "PWCS2_HIGHLIGHT_INTENT_DATABASE_PATH",
            "/data/highlights/intents.db",
        ),
    )
    parser.add_argument("--demo-storage", default="/data/demos")
    parser.add_argument("--demo-import-root", default="/data/demo-import")
    parser.add_argument(
        "--highlight-data",
        default=os.environ.get("PWCS2_HIGHLIGHT_DATA_DIR", "/data/highlights"),
    )
    parser.add_argument("--max-demo-bytes", type=int, default=512 * 1024 * 1024)
    parser.add_argument(
        "--max-backlog-demos",
        type=int,
        default=_environment_int("PWCS2_HIGHLIGHT_MAX_BACKLOG_DEMOS", 2),
    )
    parser.add_argument(
        "--max-backlog-demo-bytes",
        type=int,
        default=_environment_int(
            "PWCS2_HIGHLIGHT_MAX_BACKLOG_DEMO_BYTES",
            2 * 1024 * 1024 * 1024,
        ),
    )
    parser.add_argument("--poll-interval", type=float, default=30)
    parser.add_argument("--initial-delay", type=float, default=15)
    parser.add_argument(
        "--shared-data-gid",
        type=int,
        default=_environment_int("PWCS2_HIGHLIGHT_SHARED_OUTPUT_GID", -1),
    )
    args = parser.parse_args()
    highlight_root = Path(args.highlight_data).expanduser().resolve()
    intent_database = Path(args.database).expanduser().resolve()
    demo_storage = Path(args.demo_storage).expanduser().resolve()
    demo_import_root = Path(args.demo_import_root).expanduser().resolve()
    if intent_database.parent != highlight_root:
        raise SystemExit(
            "highlight intent database must be directly inside highlight data"
        )
    if any(
        _paths_overlap(highlight_root, path)
        for path in (demo_storage, demo_import_root)
    ) or _paths_overlap(demo_storage, demo_import_root):
        raise SystemExit("highlight data, Demo storage and import roots must be separate")
    if not 1 <= args.max_backlog_demos <= 64:
        raise SystemExit("highlight Demo backlog count must be between 1 and 64")
    if not -1 <= args.shared_data_gid <= 65535:
        raise SystemExit("highlight shared data gid is outside the supported range")
    if args.shared_data_gid < 0:
        args.shared_data_gid = None
    acquisition_reservation = _PWA_MAX_DOWNLOAD_BYTES + 2 * args.max_demo_bytes
    if not acquisition_reservation <= args.max_backlog_demo_bytes <= 16 * 1024**3:
        raise SystemExit(
            "highlight Demo backlog bytes must fit one in-flight acquisition "
            "and be at most 16 GiB"
        )
    asyncio.run(_main_async(args))


def _environment_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        raise SystemExit(f"{name} must be an integer") from None


def _paths_overlap(first: Path, second: Path) -> bool:
    try:
        first.relative_to(second)
    except ValueError:
        try:
            second.relative_to(first)
        except ValueError:
            return False
    return True


if __name__ == "__main__":
    main()
