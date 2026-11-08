"""Open and settle predictions for verified live PVP and official CS2 matches."""

from __future__ import annotations

import asyncio
import hashlib
import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from loguru import logger

from pwcs2_bot.community import (
    AutoPredictionRun,
    CommunityError,
    CommunityRepository,
    WatchStageState,
)
from pwcs2_bot.database import Binding, BindingRepository
from pwcs2_bot.match_formatting import build_match_snapshot, match_sort_key
from pwcs2_bot.perfectworld import PerfectWorldApiError, PerfectWorldClient
from pwcs2_bot.steam_presence import SteamPresence, SteamPresenceClient, SteamPresenceError
from pwcs2_bot.watch_stage import (
    WatchStageClient,
    WatchStageError,
    WatchStageSnapshot,
)

SendMessage = Callable[[str, str], Awaitable[None]]
_PVP_MATCH_ID_PATTERN = re.compile(r"^(?:PVP@)?(\d+)$", re.IGNORECASE)
_OFFICIAL_RUN_PREFIX = "live:official:"


def _normalize_pvp_match_id(value: Any) -> str:
    normalized = str(value or "").strip()
    matched = _PVP_MATCH_ID_PATTERN.fullmatch(normalized)
    return f"PVP@{int(matched.group(1))}" if matched else ""


def _expected_watch_match_id(prediction_date: str) -> str:
    prefix = "live:watch:"
    if not prediction_date.startswith(prefix):
        return ""
    parts = prediction_date.split(":", 3)
    if len(parts) != 4:
        return ""
    return _normalize_pvp_match_id(parts[3])


def _watch_roster_key(snapshot: WatchStageSnapshot) -> str:
    steam_ids = sorted(
        {
            player.steam_id
            for player in snapshot.players
            if player.steam_id.isdigit() and len(player.steam_id) == 17
        }
    )
    if not steam_ids:
        return ""
    return hashlib.sha256("|".join(steam_ids).encode()).hexdigest()[:16]


def _detail_roster_key(players: list[Any]) -> str:
    steam_ids = sorted(
        {
            str(player.get("playerId") or player.get("steamId") or "").strip()
            for player in players
            if isinstance(player, dict)
            and str(
                player.get("playerId") or player.get("steamId") or ""
            ).strip().isdigit()
            and len(
                str(
                    player.get("playerId") or player.get("steamId") or ""
                ).strip()
            )
            == 17
        }
    )
    if not steam_ids:
        return ""
    return hashlib.sha256("|".join(steam_ids).encode()).hexdigest()[:16]


def _expected_official_roster_key(prediction_date: str) -> str:
    if not prediction_date.startswith(_OFFICIAL_RUN_PREFIX):
        return ""
    parts = prediction_date.split(":")
    if len(parts) != 5:
        return ""
    roster_key = parts[3].strip().casefold()
    return roster_key if re.fullmatch(r"[0-9a-f]{16}", roster_key) else ""


class AutoPredictionService:
    def __init__(
        self,
        repository: CommunityRepository,
        bindings: BindingRepository,
        api: PerfectWorldClient,
        presence: SteamPresenceClient,
        send_message: SendMessage,
        *,
        enabled: bool = True,
        duration_minutes: int = 30,
        expiry_hours: int = 4,
        poll_interval_seconds: float = 30,
        initial_delay_seconds: float = 30,
        allowed_groups: list[str] | None = None,
        watch_stage: WatchStageClient | None = None,
        watch_stage_concurrency: int = 4,
        max_total_score: int = 3,
    ) -> None:
        self.repository = repository
        self.bindings = bindings
        self.api = api
        self.presence = presence
        self.send_message = send_message
        self.enabled = enabled
        self.duration_minutes = duration_minutes
        self.expiry_hours = expiry_hours
        self.poll_interval_seconds = poll_interval_seconds
        self.initial_delay_seconds = initial_delay_seconds
        self.allowed_groups = set(allowed_groups or [])
        self.watch_stage = watch_stage
        self.watch_stage_concurrency = max(1, watch_stage_concurrency)
        self.max_total_score = max(0, max_total_score)
        self._stop_event = asyncio.Event()
        self._legacy_retired = False
        self._presence_failure_count = 0
        self._watch_stage_failure_count = 0

    def stop(self) -> None:
        self._stop_event.set()

    async def run(self) -> None:
        if not self.enabled or await self._wait(self.initial_delay_seconds):
            return
        await self._retire_legacy_predictions()
        watch_stage_enabled = self.watch_stage is not None and self.watch_stage.enabled
        if not watch_stage_enabled and not self.presence.configured:
            logger.warning(
                "pwcs2: live auto prediction disabled because neither watch stage "
                "nor Steam presence is configured"
            )
            return
        logger.info(
            "pwcs2: live auto prediction started "
            "(primary={} poll={}s duration={}m expiry={}h)",
            "perfect-world-watch-stage" if watch_stage_enabled else "steam-presence",
            self.poll_interval_seconds,
            self.duration_minutes,
            self.expiry_hours,
        )
        while not self._stop_event.is_set():
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("pwcs2: unexpected live auto prediction failure")
            if await self._wait(self.poll_interval_seconds):
                return

    async def _wait(self, seconds: float) -> bool:
        if seconds <= 0:
            return self._stop_event.is_set()
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=seconds)
        except TimeoutError:
            return False
        return True

    async def _retire_legacy_predictions(self) -> int:
        if self._legacy_retired:
            return 0
        retired = 0
        for run in self.repository.legacy_waiting_auto_predictions():
            try:
                self.repository.cancel_auto_prediction(
                    run.prediction_id,
                    status="void",
                    result_value="竞猜策略已切换为开局竞猜",
                )
                retired += 1
            except CommunityError:
                continue
        self._legacy_retired = True
        await self._flush_pending_messages()
        return retired

    async def poll_once(self, *, now: datetime | None = None) -> tuple[int, int]:
        current = now or datetime.now(UTC)
        if current.tzinfo is None:
            current = current.replace(tzinfo=UTC)
        current = current.astimezone(UTC)
        await self._retire_legacy_predictions()

        expired = 0
        for run in self.repository.expired_auto_predictions(
            current.isoformat(timespec="seconds")
        ):
            try:
                self.repository.cancel_auto_prediction(
                    run.prediction_id,
                    status="expired",
                    result_value=f"{self.expiry_hours} 小时内没有检测到完赛记录",
                )
                expired += 1
            except CommunityError:
                continue
        await self._flush_pending_messages()

        bindings = tuple(
            binding
            for binding in self.bindings.list_bindings()
            if not self.allowed_groups or binding.group_id in self.allowed_groups
        )
        if not bindings:
            return 0, expired

        created = 0
        observed_at = current.isoformat(timespec="seconds")
        memories: dict[str, dict[str, str]] = {}
        presence_bindings = bindings
        if self.watch_stage is not None and self.watch_stage.enabled:
            steam_ids = tuple(dict.fromkeys(binding.steam_id for binding in bindings))
            snapshots, _failed_steam_ids = await self._watch_stage_snapshots(steam_ids)
            for binding in bindings:
                snapshot = snapshots.get(binding.steam_id)
                if snapshot is None:
                    continue
                if await self._observe_watch_stage(
                    binding,
                    snapshot,
                    current=current,
                    observed_at=observed_at,
                    memories=memories,
                ):
                    created += 1

        if presence_bindings and self.presence.configured:
            created += await self._poll_steam_presence(
                presence_bindings,
                observed_at=observed_at,
            )

        await self._flush_pending_messages()
        return created, expired

    async def _watch_stage_snapshots(
        self,
        steam_ids: tuple[str, ...],
    ) -> tuple[dict[str, WatchStageSnapshot], set[str]]:
        if self.watch_stage is None:
            return {}, set(steam_ids)
        semaphore = asyncio.Semaphore(self.watch_stage_concurrency)

        async def fetch(
            steam_id: str,
        ) -> tuple[str, WatchStageSnapshot | None, WatchStageError | None]:
            try:
                async with semaphore:
                    snapshot = await self.watch_stage.snapshot(steam_id)
            except (WatchStageError, ValueError) as exc:
                error = (
                    exc
                    if isinstance(exc, WatchStageError)
                    else WatchStageError(str(exc))
                )
                return steam_id, None, error
            return steam_id, snapshot, None

        results = await asyncio.gather(*(fetch(steam_id) for steam_id in steam_ids))
        snapshots = {
            steam_id: snapshot
            for steam_id, snapshot, _error in results
            if snapshot is not None
        }
        failures = {
            steam_id
            for steam_id, _snapshot, error in results
            if error is not None
        }
        if failures:
            self._watch_stage_failure_count += 1
            count = self._watch_stage_failure_count
            sample = next(
                str(error)
                for _steam_id, _snapshot, error in results
                if error is not None
            )
            log = logger.warning if count == 1 or count & (count - 1) == 0 else logger.debug
            log(
                "pwcs2: watch-stage poll partially failed "
                "(consecutive={} failed={}/{}); Steam telemetry remains read-only: {}",
                count,
                len(failures),
                len(steam_ids),
                sample,
            )
        elif self._watch_stage_failure_count:
            logger.info(
                "pwcs2: watch-stage polling recovered after {} partial failures",
                self._watch_stage_failure_count,
            )
            self._watch_stage_failure_count = 0
        return snapshots, failures

    async def _observe_watch_stage(
        self,
        binding: Binding,
        snapshot: WatchStageSnapshot,
        *,
        current: datetime,
        observed_at: str,
        memories: dict[str, dict[str, str]],
    ) -> bool:
        raw_match_id = (
            snapshot.match_id.strip() if snapshot.status == "running" else ""
        )
        previous = self.repository.watch_stage_state(
            binding.group_id, binding.steam_id
        )
        if previous is None:
            self._save_watch_stage(binding, snapshot, observed_at)
            return False
        if not raw_match_id or not self._watch_session_started(previous, snapshot):
            self._save_watch_stage(binding, snapshot, observed_at)
            return False

        current_match_id = _normalize_pvp_match_id(raw_match_id)
        target_is_playing = any(
            player.steam_id == binding.steam_id for player in snapshot.players
        )
        valid_official_roster = len(
            {
                player.steam_id
                for player in snapshot.players
                if player.steam_id.isdigit() and len(player.steam_id) == 17
            }
        ) >= 10
        is_pvp = snapshot.platform == "2" and bool(current_match_id)
        is_official = (
            snapshot.platform == "1"
            and bool(raw_match_id)
            and valid_official_roster
        )
        if not target_is_playing or not (is_pvp or is_official):
            logger.info(
                "pwcs2: ignored invalid watch-stage signal group={} steam={} "
                "platform={} match={} target_present={} official_roster={}",
                binding.group_id,
                binding.steam_id,
                snapshot.platform,
                raw_match_id,
                target_is_playing,
                valid_official_roster,
            )
            self._save_watch_stage(binding, snapshot, observed_at)
            return False

        score_is_early = (
            snapshot.ct_score is not None
            and snapshot.terrorist_score is not None
            and snapshot.ct_score >= 0
            and snapshot.terrorist_score >= 0
            and snapshot.ct_score + snapshot.terrorist_score <= self.max_total_score
        )
        if not snapshot.warmup and not score_is_early:
            logger.info(
                "pwcs2: skipped mid-game watch-stage signal group={} steam={} "
                "match={} score={}:{}",
                binding.group_id,
                binding.steam_id,
                current_match_id,
                snapshot.ct_score,
                snapshot.terrorist_score,
            )
            self._save_watch_stage(binding, snapshot, observed_at)
            return False
        if self.repository.active_prediction(binding.group_id) is not None:
            self._save_watch_stage(binding, snapshot, observed_at)
            return False

        try:
            matches = (
                await self.api.official_match_list(binding.steam_id, page_size=1)
                if is_official
                else await self.api.match_list(binding.steam_id, page_size=1)
            )
        except (PerfectWorldApiError, ValueError) as exc:
            logger.warning(
                "pwcs2: {} prediction baseline failed steam={}: {}",
                "official-match" if is_official else "PVP",
                binding.steam_id,
                exc,
            )
            return False
        latest = next(
            (match for match in matches if str(match.get("matchId") or "").strip()),
            None,
        )
        group_memories = self._group_memories(binding.group_id, memories)
        live_player = next(
            (
                player
                for player in snapshot.players
                if player.steam_id == binding.steam_id
            ),
            None,
        )
        nickname = (
            group_memories.get(binding.steam_id, "")
            or (live_player.nickname if live_player is not None else "")
            or (
                str(latest.get("steamName") or latest.get("steamNick") or "").strip()
                if latest
                else ""
            )
            or f"QQ {binding.qq_id}"
        )
        if is_official:
            roster_key = _watch_roster_key(snapshot)
            run_key = (
                f"{_OFFICIAL_RUN_PREFIX}{binding.steam_id}:"
                f"{roster_key}:{int(current.timestamp())}"
            )
        else:
            run_key = f"live:watch:{binding.steam_id}:{current_match_id}"
        kind = self._choice(binding.group_id, run_key, ("result", "kd", "rating"))
        question, options, threshold = self._question(
            nickname,
            kind,
            official=is_official,
        )
        expires_at = current + timedelta(hours=self.expiry_hours)
        try:
            self.repository.create_auto_prediction(
                binding.group_id,
                run_key,
                binding.qq_id,
                binding.steam_id,
                nickname,
                str(latest.get("matchId") or "") if latest else "",
                match_sort_key(latest)[0] if latest else 0,
                kind,
                threshold,
                question,
                options,
                duration_minutes=self.duration_minutes,
                expires_at=expires_at.isoformat(timespec="seconds"),
            )
        except CommunityError as exc:
            logger.info(
                "pwcs2: skipped watch-stage prediction group={} steam={}: {}",
                binding.group_id,
                binding.steam_id,
                exc,
            )
            created = False
        else:
            created = True
        self._save_watch_stage(binding, snapshot, observed_at)
        return created

    async def _poll_steam_presence(
        self,
        bindings: tuple[Binding, ...],
        *,
        observed_at: str,
    ) -> int:
        try:
            summaries = await self.presence.summaries(
                tuple(dict.fromkeys(binding.steam_id for binding in bindings))
            )
        except SteamPresenceError as exc:
            self._presence_failure_count += 1
            failures = self._presence_failure_count
            if failures == 1 or failures & (failures - 1) == 0:
                logger.warning(
                    "pwcs2: Steam presence poll failed (consecutive={}): {}",
                    failures,
                    exc,
                )
            else:
                logger.debug(
                    "pwcs2: Steam presence poll still failing (consecutive={}): {}",
                    failures,
                    exc,
                )
            return 0
        if self._presence_failure_count:
            logger.info(
                "pwcs2: Steam presence polling recovered after {} failures",
                self._presence_failure_count,
            )
            self._presence_failure_count = 0

        for binding in bindings:
            presence = summaries.get(binding.steam_id)
            if presence is None:
                continue
            previous = self.repository.steam_presence_state(
                binding.group_id, binding.steam_id
            )
            if previous is None:
                self._save_presence(binding, presence, observed_at)
                continue
            self._save_presence(binding, presence, observed_at)

        return 0

    def _group_memories(
        self,
        group_id: str,
        cache: dict[str, dict[str, str]],
    ) -> dict[str, str]:
        memories = cache.get(group_id)
        if memories is None:
            memories = {
                memory.steam_id: memory.nickname
                for memory in self.bindings.list_group_member_memories(group_id)
            }
            cache[group_id] = memories
        return memories

    @staticmethod
    def _watch_session_started(
        previous: WatchStageState,
        snapshot: WatchStageSnapshot,
    ) -> bool:
        raw_match_id = snapshot.match_id.strip()
        if not raw_match_id:
            return False
        if previous.match_id != raw_match_id or previous.platform != snapshot.platform:
            return True
        if snapshot.platform != "1":
            return False

        roster_key = _watch_roster_key(snapshot)
        if (
            previous.map_name
            and snapshot.map_name
            and previous.map_name.casefold() != snapshot.map_name.casefold()
        ):
            return True
        if previous.roster_key and roster_key and previous.roster_key != roster_key:
            return True
        scores = (
            previous.ct_score,
            previous.terrorist_score,
            snapshot.ct_score,
            snapshot.terrorist_score,
        )
        if all(value is not None and value >= 0 for value in scores):
            previous_total = int(previous.ct_score or 0) + int(
                previous.terrorist_score or 0
            )
            current_total = int(snapshot.ct_score or 0) + int(
                snapshot.terrorist_score or 0
            )
            return (
                previous_total >= 10
                and current_total <= 3
                and current_total < previous_total
            )
        return False

    def _save_watch_stage(
        self,
        binding: Binding,
        snapshot: WatchStageSnapshot,
        observed_at: str,
    ) -> None:
        running = snapshot.status == "running"
        self.repository.set_watch_stage_state(
            binding.group_id,
            binding.steam_id,
            snapshot.match_id.strip() if running else "",
            platform=snapshot.platform if running else "",
            map_name=snapshot.map_name if running else "",
            ct_score=snapshot.ct_score if running else None,
            terrorist_score=snapshot.terrorist_score if running else None,
            roster_key=_watch_roster_key(snapshot) if running else "",
            observed_at=observed_at,
        )

    def _save_presence(
        self, binding: Binding, presence: SteamPresence, observed_at: str
    ) -> None:
        self.repository.set_steam_presence_state(
            binding.group_id,
            binding.steam_id,
            presence.game_id,
            presence.server_ip,
            observed_at=observed_at,
        )

    async def observe_matches(
        self,
        group_id: str,
        steam_id: str,
        matches: list[dict[str, Any]],
        data_source: int = 3,
    ) -> None:
        if not self.enabled:
            return
        run = self.repository.waiting_auto_prediction(group_id, steam_id)
        if run is None or not run.prediction_date.startswith("live:"):
            return
        now = datetime.now(UTC)
        if datetime.fromisoformat(run.expires_at) <= now:
            try:
                self.repository.cancel_auto_prediction(
                    run.prediction_id,
                    status="expired",
                    result_value=f"{self.expiry_hours} 小时内没有检测到完赛记录",
                )
            except CommunityError:
                return
            await self._flush_pending_messages()
            return

        official = run.prediction_date.startswith(_OFFICIAL_RUN_PREFIX)
        if official:
            if data_source != 1:
                return
            matching = [
                match
                for match in matches
                if str(match.get("matchId") or "").strip()
                and str(match.get("matchId") or "").strip()
                != run.baseline_match_id
                and (
                    run.baseline_timestamp <= 0
                    or match_sort_key(match)[0] > run.baseline_timestamp
                )
            ]
        else:
            if data_source != 3:
                return
            expected_match_id = _expected_watch_match_id(run.prediction_date)
            if not expected_match_id:
                return
            matching = [
                match
                for match in matches
                if _normalize_pvp_match_id(match.get("matchId"))
                == expected_match_id
            ]
        if not matching:
            return
        if official:
            expected_roster_key = _expected_official_roster_key(
                run.prediction_date
            )
            if not expected_roster_key:
                logger.warning(
                    "pwcs2: official prediction has no valid roster key id={}",
                    run.prediction_id,
                )
                return
            selected: tuple[dict[str, Any], str, dict[str, Any]] | None = None
            for candidate in sorted(matching, key=match_sort_key):
                candidate_id = str(candidate.get("matchId") or "").strip()
                try:
                    candidate_detail = await self.api.match_detail(
                        candidate_id,
                        data_source=1,
                    )
                except (PerfectWorldApiError, ValueError) as exc:
                    logger.warning(
                        "pwcs2: official prediction detail failed match={}: {}",
                        candidate_id,
                        exc,
                    )
                    return
                players = (
                    candidate_detail.get("players")
                    if isinstance(candidate_detail, dict)
                    else None
                )
                if not isinstance(players, list) or len(players) < 10:
                    logger.info(
                        "pwcs2: official match detail not ready for prediction "
                        "match={} players={}",
                        candidate_id,
                        len(players) if isinstance(players, list) else 0,
                    )
                    return
                if _detail_roster_key(players) != expected_roster_key:
                    continue
                selected = (candidate, candidate_id, candidate_detail)
                break
            if selected is None:
                logger.warning(
                    "pwcs2: no official result matched live roster "
                    "steam={} candidates={}",
                    steam_id,
                    len(matching),
                )
                return
            raw_match, match_id, detail = selected
        else:
            raw_match = max(matching, key=match_sort_key)
            match_id = str(raw_match.get("matchId") or "").strip()
            detail: dict[str, Any] | None = None
            try:
                detail = await self.api.match_detail(
                    match_id,
                    data_source=3,
                )
            except (PerfectWorldApiError, ValueError) as exc:
                logger.warning(
                    "pwcs2: auto prediction match detail failed match={}: {}",
                    match_id,
                    exc,
                )
        snapshot = build_match_snapshot(raw_match, steam_id=steam_id, detail=detail)
        resolved = self._resolve(run, snapshot)
        if resolved is None:
            try:
                self.repository.cancel_auto_prediction(
                    run.prediction_id,
                    status="void",
                    result_value="比赛数据不完整，无法判定，已退款",
                )
            except CommunityError:
                return
        else:
            option, value = resolved
            try:
                self.repository.settle_auto_prediction(
                    run.prediction_id,
                    option,
                    match_id=match_id,
                    result_value=value,
                )
            except CommunityError:
                return
        await self._flush_pending_messages()

    @staticmethod
    def _resolve(run: AutoPredictionRun, snapshot: Any) -> tuple[int, str] | None:
        if run.kind == "result":
            if snapshot.result == "finished":
                return None
            option = 1 if snapshot.result == "win" else 2
            score = f"{snapshot.own_score}:{snapshot.opponent_score}"
            return option, f"{snapshot.result_label}（{score}）"
        field = snapshot.kd if run.kind == "kd" else snapshot.rating
        try:
            value = float(field)
        except (TypeError, ValueError):
            if run.kind != "kd":
                return None
            try:
                kills = float(snapshot.kills)
                deaths = float(snapshot.deaths)
            except (TypeError, ValueError):
                return None
            if deaths != 0:
                return None
            value = kills
        threshold = run.threshold or 0.0
        option = 1 if value >= threshold else 2
        label = "K/D" if run.kind == "kd" else "Rating"
        return option, f"{label} {value:.2f}"

    async def _flush_pending_messages(self) -> None:
        for run in self.repository.pending_auto_announcements():
            if self.allowed_groups and run.group_id not in self.allowed_groups:
                continue
            prediction = self.repository.prediction_by_id(run.prediction_id)
            if prediction is None:
                continue
            official = run.prediction_date.startswith(_OFFICIAL_RUN_PREFIX)
            if official:
                presence_line = (
                    f"🛰️ 完美观将台检测到 {run.nickname} 已进入官匹"
                )
            elif run.prediction_date.startswith("live:watch:"):
                presence_line = (
                    f"👁️ 完美观将台检测到 {run.nickname} 已进入新对局"
                )
            elif run.prediction_date.startswith("live:launch:"):
                presence_line = (
                    f"🎮 检测到 {run.nickname} 已启动 CS2"
                    "（Steam 未提供服务器地址，按下一场完美平台比赛开盘）"
                )
            else:
                presence_line = f"🎮 检测到 {run.nickname} 已进入 CS2 服务器"
            lines = [
                presence_line,
                f"🤖 开局竞猜 #{prediction.prediction_id}",
                prediction.question,
                *(f"{index}. {option}" for index, option in enumerate(prediction.options, 1)),
                f"下注开放 {self.duration_minutes} 分钟｜#下注 <编号> <牛币>",
                (
                    f"比赛结束并出现{'官匹' if official else '完美平台'}战绩后"
                    f"自动开奖；{self.expiry_hours} 小时无记录自动退款。"
                ),
            ]
            try:
                await self.send_message(run.group_id, "\n".join(lines))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "pwcs2: auto prediction announcement failed group={}: {}",
                    run.group_id,
                    exc,
                )
                continue
            self.repository.mark_auto_announcement_sent(run.prediction_id)

        for run in self.repository.pending_auto_results():
            if self.allowed_groups and run.group_id not in self.allowed_groups:
                continue
            prediction = self.repository.prediction_by_id(run.prediction_id)
            if prediction is None:
                continue
            bets = self.repository.prediction_bets(run.prediction_id)
            pool = sum(stake for _qq, _option, stake in bets)
            if run.status == "settled" and run.winning_option is not None:
                winners = sum(1 for _qq, option, _stake in bets if option == run.winning_option)
                answer = prediction.options[run.winning_option - 1]
                if winners:
                    detail = f"奖池 {pool} 牛币由 {winners} 位赢家按下注比例瓜分。"
                else:
                    detail = "无人猜中，所有下注已原路退回。"
                lines = [
                    f"🏆 开局竞猜 #{run.prediction_id} 开奖",
                    f"{run.nickname} 的比赛结果：{run.result_value}",
                    f"答案：{run.winning_option}. {answer}",
                    detail,
                ]
                insurance_refunds = self.repository.prediction_insurance_refunds(
                    run.prediction_id
                )
                if insurance_refunds:
                    lines.append(
                        f"🛡️ 竞猜保险为 {len(insurance_refunds)} 人理赔 "
                        f"{sum(amount for _qq, amount in insurance_refunds)} 牛币。"
                    )
            else:
                lines = [
                    f"↩️ 自动竞猜 #{run.prediction_id} 已取消",
                    f"原因：{run.result_value}",
                    f"已向 {len(bets)} 位参与者退还全部下注。",
                ]
            try:
                await self.send_message(run.group_id, "\n".join(lines))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "pwcs2: auto prediction result send failed group={}: {}",
                    run.group_id,
                    exc,
                )
                continue
            self.repository.mark_auto_result_sent(run.prediction_id)

    @staticmethod
    def _question(
        nickname: str,
        kind: str,
        *,
        official: bool = False,
    ) -> tuple[str, tuple[str, ...], float | None]:
        if kind == "kd":
            return f"{nickname} 这场比赛 K/D 能否达到 1.00？", ("能", "不能"), 1.0
        if kind == "rating":
            return f"{nickname} 这场比赛 Rating 能否达到 1.10？", ("能", "不能"), 1.1
        platform = "官匹" if official else "完美平台比赛"
        return f"{nickname} 这场{platform}能否获胜？", ("胜利", "未胜（负或平）"), None

    @staticmethod
    def _choice(group_id: str, run_key: str, values: tuple[str, ...]) -> str:
        digest = hashlib.sha256(
            f"{group_id}:{run_key}:kind:auto-prediction".encode()
        ).digest()
        return values[int.from_bytes(digest[:8], "big") % len(values)]
