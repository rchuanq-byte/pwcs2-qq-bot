from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from pwcs2_bot.daily_word_cloud import (
    DailyWordCloudService,
    WordCloudSnapshot,
    WordCloudWord,
    render_word_cloud_html,
    tokenize_message,
)
from pwcs2_bot.database import BindingRepository

TZ = ZoneInfo("Asia/Shanghai")


def test_tokenizer_removes_urls_ids_and_stopwords() -> None:
    counts = tokenize_message(
        "今天 CS2 CS2 沙二真的好玩 https://example.com 76561197960266101"
    )

    assert counts["cs2"] == 2
    assert "今天" not in counts
    assert "真的" not in counts
    assert all("http" not in word for word in counts)
    assert all("765611" not in word for word in counts)


async def test_daily_word_cloud_records_aggregates_and_sends_once(
    tmp_path: Path,
) -> None:
    path = tmp_path / "bindings.db"
    repository = BindingRepository(path)
    sent: list[tuple[str, WordCloudSnapshot]] = []

    async def send(group_id: str, snapshot: WordCloudSnapshot) -> None:
        sent.append((group_id, snapshot))

    service = DailyWordCloudService(
        repository,
        send,
        allowed_groups=["group"],
    )
    morning = datetime(2026, 7, 3, 10, 0, tzinfo=TZ)
    assert service.record_message("group", "今晚一起开黑，荒漠迷城开黑", now=morning) > 0
    assert service.record_message("group", "荒漠迷城战术讨论", now=morning) > 0

    assert repository.get_daily_word_cloud_activity("group", "2026-07-03")[0] == 2
    assert await service.run_due_once(datetime(2026, 7, 3, 23, 29, tzinfo=TZ)) == 0
    assert await service.run_due_once(datetime(2026, 7, 3, 23, 30, tzinfo=TZ)) == 1
    assert await service.run_due_once(datetime(2026, 7, 3, 23, 31, tzinfo=TZ)) == 0

    assert len(sent) == 1
    group_id, snapshot = sent[0]
    assert group_id == "group"
    assert snapshot.cloud_date == "2026-07-03"
    assert snapshot.message_count == 2
    assert snapshot.token_count > 0
    assert any(
        "荒漠" in word.text or "迷城" in word.text
        for word in snapshot.words
    )
    assert repository.is_daily_word_cloud_processed("group", "2026-07-03")
    assert "今晚一起开黑".encode() not in path.read_bytes()


async def test_empty_word_cloud_day_is_marked_without_sending(tmp_path: Path) -> None:
    repository = BindingRepository(tmp_path / "bindings.db")

    async def unexpected_send(_group_id: str, _snapshot: WordCloudSnapshot) -> None:
        raise AssertionError("empty day must not send")

    service = DailyWordCloudService(
        repository,
        unexpected_send,
        allowed_groups=["group"],
    )
    now = datetime(2026, 7, 3, 23, 30, tzinfo=TZ)

    assert await service.run_due_once(now) == 0
    assert repository.is_daily_word_cloud_processed("group", "2026-07-03")


def test_word_cloud_html_escapes_words_and_contains_privacy_note() -> None:
    snapshot = WordCloudSnapshot(
        group_id="group",
        cloud_date="2026-07-03",
        message_count=12,
        token_count=40,
        words=(WordCloudWord("<script>", 8), WordCloudWord("CS2", 5)),
    )

    document = render_word_cloud_html(snapshot)

    assert "<script>" not in document
    assert "&lt;script&gt;" in document
    assert "不保存聊天原文" in document
    assert 'id="word-cloud"' in document
