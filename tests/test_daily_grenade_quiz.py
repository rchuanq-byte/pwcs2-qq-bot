from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from pwcs2_bot.community import (
    LEGACY_PRO_PLAYER_QUESTIONS_V1,
    CommunityRepository,
)
from pwcs2_bot.daily_grenade_quiz import DailyGrenadeQuizService
from pwcs2_bot.database import BindingRepository

TZ = ZoneInfo("Asia/Shanghai")


def _service(tmp_path: Path):
    path = tmp_path / "bindings.db"
    bindings = BindingRepository(path)
    repository = CommunityRepository(path)
    questions = []
    reveals = []

    async def send_question(group_id, question) -> None:
        questions.append((group_id, question))

    async def send_reveal(group_id, reveal) -> None:
        reveals.append((group_id, reveal))

    service = DailyGrenadeQuizService(
        repository,
        bindings,
        send_question,
        send_reveal,
        initial_delay_seconds=0,
        allowed_groups=["group"],
    )
    return repository, service, questions, reveals


async def test_quiz_posts_once_accepts_answer_and_reveals(tmp_path: Path) -> None:
    repository, service, questions, reveals = _service(tmp_path)

    assert await service.poll_once(
        now=datetime(2026, 7, 22, 19, 29, tzinfo=TZ)
    ) == (0, 0)
    assert await service.poll_once(
        now=datetime(2026, 7, 22, 19, 30, tzinfo=TZ)
    ) == (1, 0)
    question = questions[0][1]
    repository.submit_quiz_answer(
        "group", "2026-07-22", "10001", "牛牛", question.answer
    )
    assert await service.poll_once(
        now=datetime(2026, 7, 22, 21, 0, tzinfo=TZ)
    ) == (0, 1)
    assert await service.poll_once(
        now=datetime(2026, 7, 22, 21, 5, tzinfo=TZ)
    ) == (0, 0)

    assert reveals[0][1].correct_names == ("牛牛",)
    assert repository.wallet("group", "10001")[0] == 130


async def test_quiz_skips_day_if_bot_missed_answer_window(tmp_path: Path) -> None:
    repository, service, questions, reveals = _service(tmp_path)

    assert await service.poll_once(
        now=datetime(2026, 7, 22, 21, 5, tzinfo=TZ)
    ) == (0, 0)

    run = repository.quiz_run("group", "2026-07-22")
    assert run is not None
    assert int(run["revealed"]) == 1
    assert questions == []
    assert reveals == []


async def test_quiz_retry_sends_the_question_key_already_locked_in_db(
    tmp_path: Path,
) -> None:
    repository, service, questions, _reveals = _service(tmp_path)
    locked_question = LEGACY_PRO_PLAYER_QUESTIONS_V1[0]
    repository.ensure_quiz_run("group", "2026-08-20", locked_question.key)

    assert await service.poll_once(
        now=datetime(2026, 8, 20, 19, 30, tzinfo=TZ)
    ) == (1, 0)

    assert questions == [("group", locked_question)]
