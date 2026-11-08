from datetime import UTC, datetime, timedelta
from pathlib import Path

from pwcs2_bot.database import BindingRepository


def _snapshot(*, live: bool, session: str = "") -> dict[str, object]:
    return {
        "platform": "douyu",
        "room_id": "6979222",
        "streamer_name": "玩机器",
        "is_live": live,
        "session_key": session,
        "title": "小众宝藏结晶直播间",
        "category": "CS2",
        "cover_url": "",
        "live_url": "https://www.douyu.com/6657",
        "started_at": "2026-08-12 16:34:43" if live else "",
    }


def test_first_live_observation_only_creates_baseline(tmp_path: Path) -> None:
    repository = BindingRepository(tmp_path / "bot.db")

    transitioned = repository.record_stream_live_observation(
        "douyu",
        "6979222",
        is_live=True,
        session_key="2026-08-12 16:34:43",
        snapshot=_snapshot(live=True, session="2026-08-12 16:34:43"),
        group_ids=("123",),
        confirmation_polls=2,
    )

    assert transitioned is False
    assert repository.claim_stream_live_notifications() == ()
    assert repository.stream_live_state("douyu", "6979222")["confirmed_live"] is True


def test_offline_to_live_requires_confirmation_and_notifies_each_group_once(
    tmp_path: Path,
) -> None:
    repository = BindingRepository(tmp_path / "bot.db")
    repository.record_stream_live_observation(
        "douyu",
        "6979222",
        is_live=False,
        session_key="",
        snapshot=_snapshot(live=False),
        group_ids=("123", "456"),
    )

    first = repository.record_stream_live_observation(
        "douyu",
        "6979222",
        is_live=True,
        session_key="session-a",
        snapshot=_snapshot(live=True, session="session-a"),
        group_ids=("123", "456"),
    )
    second = repository.record_stream_live_observation(
        "douyu",
        "6979222",
        is_live=True,
        session_key="session-a",
        snapshot=_snapshot(live=True, session="session-a"),
        group_ids=("123", "456"),
    )

    assert first is False
    assert second is True
    claimed = repository.claim_stream_live_notifications()
    assert {item.group_id for item in claimed} == {"123", "456"}
    assert all(item.session_key == "session-a" for item in claimed)
    for item in claimed:
        repository.mark_stream_live_notification_sent(item)
    assert repository.claim_stream_live_notifications() == ()

    repository.record_stream_live_observation(
        "douyu",
        "6979222",
        is_live=True,
        session_key="session-a",
        snapshot=_snapshot(live=True, session="session-a"),
        group_ids=("123", "456"),
    )
    assert repository.claim_stream_live_notifications() == ()


def test_restart_does_not_requeue_live_notification(tmp_path: Path) -> None:
    path = tmp_path / "bot.db"
    first_repository = BindingRepository(path)
    first_repository.record_stream_live_observation(
        "douyu",
        "6979222",
        is_live=False,
        session_key="",
        snapshot=_snapshot(live=False),
        group_ids=("123",),
        confirmation_polls=1,
    )
    first_repository.record_stream_live_observation(
        "douyu",
        "6979222",
        is_live=True,
        session_key="session-a",
        snapshot=_snapshot(live=True, session="session-a"),
        group_ids=("123",),
        confirmation_polls=1,
    )
    claimed = first_repository.claim_stream_live_notifications()
    assert len(claimed) == 1
    first_repository.mark_stream_live_notification_sent(claimed[0])

    restarted_repository = BindingRepository(path)
    restarted_repository.record_stream_live_observation(
        "douyu",
        "6979222",
        is_live=True,
        session_key="session-a",
        snapshot=_snapshot(live=True, session="session-a"),
        group_ids=("123",),
        confirmation_polls=1,
    )
    assert restarted_repository.claim_stream_live_notifications() == ()


def test_new_session_while_still_live_can_create_a_new_edge(tmp_path: Path) -> None:
    repository = BindingRepository(tmp_path / "bot.db")
    repository.record_stream_live_observation(
        "douyu",
        "6979222",
        is_live=True,
        session_key="session-a",
        snapshot=_snapshot(live=True, session="session-a"),
        group_ids=("123",),
        confirmation_polls=1,
    )

    transitioned = repository.record_stream_live_observation(
        "douyu",
        "6979222",
        is_live=True,
        session_key="session-b",
        snapshot=_snapshot(live=True, session="session-b"),
        group_ids=("123",),
        confirmation_polls=1,
    )

    assert transitioned is True
    claimed = repository.claim_stream_live_notifications()
    assert len(claimed) == 1
    assert claimed[0].session_key == "session-b"


def test_fresh_sending_claim_is_not_reclaimed_after_restart(tmp_path: Path) -> None:
    path = tmp_path / "bot.db"
    repository = BindingRepository(path)
    repository.record_stream_live_observation(
        "douyu",
        "6979222",
        is_live=False,
        session_key="",
        snapshot=_snapshot(live=False),
        group_ids=("123",),
        confirmation_polls=1,
    )
    repository.record_stream_live_observation(
        "douyu",
        "6979222",
        is_live=True,
        session_key="session-a",
        snapshot=_snapshot(live=True, session="session-a"),
        group_ids=("123",),
        confirmation_polls=1,
    )
    assert len(repository.claim_stream_live_notifications()) == 1

    assert BindingRepository(path).claim_stream_live_notifications() == ()


def test_stale_sending_claim_is_reclaimed_after_restart(tmp_path: Path) -> None:
    path = tmp_path / "bot.db"
    repository = BindingRepository(path)
    repository.record_stream_live_observation(
        "douyu",
        "6979222",
        is_live=False,
        session_key="",
        snapshot=_snapshot(live=False),
        group_ids=("123",),
        confirmation_polls=1,
    )
    repository.record_stream_live_observation(
        "douyu",
        "6979222",
        is_live=True,
        session_key="session-a",
        snapshot=_snapshot(live=True, session="session-a"),
        group_ids=("123",),
        confirmation_polls=1,
    )
    first = repository.claim_stream_live_notifications(lease_seconds=300)
    assert first[0].attempt_count == 1
    stale = (datetime.now(UTC) - timedelta(minutes=10)).isoformat(timespec="seconds")
    with repository._connect() as connection:  # noqa: SLF001 - persistence test
        connection.execute(
            "UPDATE stream_live_notifications SET claimed_at = ?",
            (stale,),
        )

    reclaimed = BindingRepository(path).claim_stream_live_notifications(
        lease_seconds=300
    )
    assert len(reclaimed) == 1
    assert reclaimed[0].attempt_count == 2


def test_failure_retries_with_backoff_and_stops_after_limit(tmp_path: Path) -> None:
    repository = BindingRepository(tmp_path / "bot.db")
    repository.record_stream_live_observation(
        "douyu",
        "6979222",
        is_live=False,
        session_key="",
        snapshot=_snapshot(live=False),
        group_ids=("123",),
        confirmation_polls=1,
    )
    repository.record_stream_live_observation(
        "douyu",
        "6979222",
        is_live=True,
        session_key="session-a",
        snapshot=_snapshot(live=True, session="session-a"),
        group_ids=("123",),
        confirmation_polls=1,
    )
    first = repository.claim_stream_live_notifications()[0]
    assert repository.retry_stream_live_notification(
        first, "temporary", base_delay_seconds=60, max_attempts=2
    )
    assert repository.claim_stream_live_notifications() == ()

    with repository._connect() as connection:  # noqa: SLF001 - persistence test
        connection.execute(
            "UPDATE stream_live_notifications SET next_attempt_at = ''"
        )
    second = repository.claim_stream_live_notifications()[0]
    assert second.attempt_count == 2
    assert not repository.retry_stream_live_notification(
        second, "still broken", base_delay_seconds=0, max_attempts=2
    )
    assert repository.claim_stream_live_notifications() == ()


def test_malformed_payload_is_failed_before_claim(tmp_path: Path) -> None:
    repository = BindingRepository(tmp_path / "bot.db")
    now = datetime.now(UTC).isoformat(timespec="seconds")
    with repository._connect() as connection:  # noqa: SLF001 - persistence test
        connection.execute(
            """
            INSERT INTO stream_live_notifications
                (group_id, platform, room_id, session_key, snapshot_json,
                 status, created_at)
            VALUES ('123', 'douyu', '1', 'bad', '{', 'pending', ?)
            """,
            (now,),
        )
    assert repository.claim_stream_live_notifications() == ()
    with repository._connect() as connection:  # noqa: SLF001
        row = connection.execute(
            "SELECT status, attempt_count FROM stream_live_notifications"
        ).fetchone()
    assert row["status"] == "failed"
    assert row["attempt_count"] == 0
