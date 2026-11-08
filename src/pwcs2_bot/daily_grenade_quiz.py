"""Schedule one Counter-Strike pro-player question and reveal per QQ group."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime, time
from zoneinfo import ZoneInfo

from loguru import logger

from pwcs2_bot.community import (
    CommunityError,
    CommunityRepository,
    QuizQuestion,
    QuizReveal,
    question_by_key,
    question_for,
)
from pwcs2_bot.database import BindingRepository

SendQuestion = Callable[[str, QuizQuestion], Awaitable[None]]
SendReveal = Callable[[str, QuizReveal], Awaitable[None]]


class DailyGrenadeQuizService:
    def __init__(
        self,
        repository: CommunityRepository,
        bindings: BindingRepository,
        send_question: SendQuestion,
        send_reveal: SendReveal,
        *,
        enabled: bool = True,
        question_time: str = "19:30",
        reveal_time: str = "21:00",
        timezone_name: str = "Asia/Shanghai",
        poll_interval_seconds: float = 300,
        initial_delay_seconds: float = 45,
        correct_reward: int = 20,
        fastest_bonus: int = 10,
        allowed_groups: list[str] | None = None,
    ) -> None:
        question_hour, question_minute = (
            int(part) for part in question_time.split(":", 1)
        )
        reveal_hour, reveal_minute = (
            int(part) for part in reveal_time.split(":", 1)
        )
        self.repository = repository
        self.bindings = bindings
        self.send_question = send_question
        self.send_reveal = send_reveal
        self.enabled = enabled
        self.question_time = time(question_hour, question_minute)
        self.reveal_time = time(reveal_hour, reveal_minute)
        if self.reveal_time <= self.question_time:
            raise ValueError("quiz reveal time must be after question time")
        self.timezone = ZoneInfo(timezone_name)
        self.poll_interval_seconds = poll_interval_seconds
        self.initial_delay_seconds = initial_delay_seconds
        self.correct_reward = correct_reward
        self.fastest_bonus = fastest_bonus
        self.allowed_groups = set(allowed_groups or [])
        self._stop_event = asyncio.Event()

    def stop(self) -> None:
        self._stop_event.set()

    async def run(self) -> None:
        if not self.enabled or await self._wait(self.initial_delay_seconds):
            return
        logger.info(
            "pwcs2: daily pro-player quiz started (question={} reveal={} timezone={})",
            self.question_time.strftime("%H:%M"),
            self.reveal_time.strftime("%H:%M"),
            self.timezone.key,
        )
        while not self._stop_event.is_set():
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("pwcs2: unexpected pro-player quiz failure")
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

    async def poll_once(self, *, now: datetime | None = None) -> tuple[int, int]:
        local_now = (now or datetime.now(self.timezone)).astimezone(self.timezone)
        if local_now.time() < self.question_time:
            return 0, 0
        quiz_date = local_now.date().isoformat()
        groups = set(self.allowed_groups)
        if not groups:
            groups = {binding.group_id for binding in self.bindings.list_bindings()}
        questions_sent = 0
        reveals_sent = 0
        for group_id in sorted(groups):
            planned_question = question_for(group_id, quiz_date)
            self.repository.ensure_quiz_run(
                group_id, quiz_date, planned_question.key
            )
            run = self.repository.quiz_run(group_id, quiz_date)
            if run is None:
                continue
            try:
                question = question_by_key(str(run["question_key"]))
            except CommunityError as exc:
                logger.warning(
                    "pwcs2: stored pro-player quiz question is unavailable "
                    "group={} date={}: {}",
                    group_id,
                    quiz_date,
                    exc,
                )
                continue
            if not int(run["question_sent"]):
                if local_now.time() >= self.reveal_time:
                    self.repository.skip_quiz(group_id, quiz_date)
                    continue
                try:
                    await self.send_question(group_id, question)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.warning(
                        "pwcs2: pro-player quiz question send failed group={}: {}",
                        group_id,
                        exc,
                    )
                    continue
                self.repository.mark_quiz_question_sent(group_id, quiz_date)
                questions_sent += 1
                run = self.repository.quiz_run(group_id, quiz_date)
            if local_now.time() >= self.reveal_time and run is not None:
                try:
                    if not int(run["revealed"]):
                        reveal = self.repository.reveal_quiz(
                            group_id,
                            quiz_date,
                            question,
                            correct_reward=self.correct_reward,
                            fastest_bonus=self.fastest_bonus,
                        )
                    elif not int(run["reveal_sent"]):
                        reveal = self.repository.quiz_reveal_snapshot(
                            group_id, quiz_date, question
                        )
                    else:
                        continue
                    await self.send_reveal(group_id, reveal)
                except asyncio.CancelledError:
                    raise
                except CommunityError:
                    continue
                except Exception as exc:
                    logger.warning(
                        "pwcs2: pro-player quiz reveal failed group={}: {}",
                        group_id,
                        exc,
                    )
                    continue
                self.repository.mark_quiz_reveal_sent(group_id, quiz_date)
                reveals_sent += 1
        return questions_sent, reveals_sent
