import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from pwcs2_bot.database import (
    BindingConflictError,
    BindingRepository,
    ValorantBindingConflictError,
)


def test_bind_update_and_group_isolation(tmp_path: Path) -> None:
    repository = BindingRepository(tmp_path / "bindings.db")

    first = repository.bind("group-1", "qq-1", "76561198929215155")
    repository.bind("group-2", "qq-1", "76561198000000001")
    updated = repository.bind("group-1", "qq-1", "76561198000000002")

    assert first.steam_id == "76561198929215155"
    assert updated.created_at == first.created_at
    assert repository.get_by_qq("group-1", "qq-1").steam_id == "76561198000000002"
    assert repository.get_by_qq("group-2", "qq-1").steam_id == "76561198000000001"


def test_same_steam_cannot_be_claimed_twice_in_group(tmp_path: Path) -> None:
    repository = BindingRepository(tmp_path / "bindings.db")
    repository.bind("group", "qq-1", "76561198929215155")

    with pytest.raises(BindingConflictError, match="其他成员"):
        repository.bind("group", "qq-2", "76561198929215155")

    repository.bind("another-group", "qq-2", "76561198929215155")


def test_unbind_reports_whether_a_row_was_deleted(tmp_path: Path) -> None:
    repository = BindingRepository(tmp_path / "bindings.db")
    repository.bind("group", "qq", "76561198929215155")

    assert repository.unbind("group", "qq") is True
    assert repository.unbind("group", "qq") is False


def test_group_ai_memory_persists_with_author_and_is_group_scoped(
    tmp_path: Path,
) -> None:
    path = tmp_path / "bindings.db"
    repository = BindingRepository(path)
    first = repository.add_group_ai_memory(
        "group-1",
        "  每周五   晚上十点开黑  ",
        created_by_qq="100001",
        created_by_nickname=" Alice ",
    )
    second = repository.add_group_ai_memory(
        "group-2",
        "另一个群的记忆",
        created_by_qq="200001",
        created_by_nickname="Bob",
    )

    reopened = BindingRepository(path)
    group_one = reopened.list_group_ai_memories("group-1")

    assert group_one == (first,)
    assert group_one[0].content == "每周五 晚上十点开黑"
    assert group_one[0].created_by_nickname == "Alice"
    assert reopened.get_group_ai_memory("group-1", first.memory_id) == first
    assert reopened.get_group_ai_memory("group-1", second.memory_id) is None
    assert reopened.list_group_ai_memories("group-2") == (second,)


def test_group_ai_memory_delete_is_scoped_and_reports_missing(tmp_path: Path) -> None:
    repository = BindingRepository(tmp_path / "bindings.db")
    entry = repository.add_group_ai_memory(
        "group-1",
        "只属于一群",
        created_by_qq="100001",
        created_by_nickname="Alice",
    )

    assert repository.delete_group_ai_memory("group-2", entry.memory_id) is False
    assert repository.get_group_ai_memory("group-1", entry.memory_id) == entry
    assert repository.delete_group_ai_memory("group-1", entry.memory_id) is True
    assert repository.delete_group_ai_memory("group-1", entry.memory_id) is False
    assert repository.get_group_ai_memory("group-1", entry.memory_id) is None


def test_valorant_bind_update_unbind_and_group_isolation(tmp_path: Path) -> None:
    repository = BindingRepository(tmp_path / "bindings.db")

    first = repository.bind_valorant("group-1", "qq-1", "ExamplePlayer#12345")
    repository.bind_valorant("group-2", "qq-2", "ExamplePlayer#12345")
    updated = repository.bind_valorant("group-1", "qq-1", "new name#12345")

    assert updated.created_at == first.created_at
    assert repository.get_valorant_by_qq("group-1", "qq-1") == updated
    assert repository.get_valorant_by_id("group-1", "ExamplePlayer#12345") is None
    assert repository.get_valorant_by_id("group-1", "NEW NAME#12345") == updated
    assert repository.get_valorant_by_id("group-2", "ExamplePlayer#12345") is not None
    assert repository.unbind_valorant("group-1", "qq-1") is True
    assert repository.unbind_valorant("group-1", "qq-1") is False
    assert repository.get_valorant_by_qq("group-2", "qq-2") is not None


def test_same_valorant_id_cannot_be_claimed_twice_in_group(tmp_path: Path) -> None:
    repository = BindingRepository(tmp_path / "bindings.db")
    repository.bind_valorant("group", "qq-1", "ExamplePlayer#12345")

    with pytest.raises(ValorantBindingConflictError, match="其他成员"):
        repository.bind_valorant("group", "qq-2", "ExamplePlayer＃60450")

    repository.bind_valorant("another-group", "qq-2", "ExamplePlayer＃60450")


def test_valorant_cache_round_trips_and_updates_by_normalized_id(
    tmp_path: Path,
) -> None:
    repository = BindingRepository(tmp_path / "bindings.db")
    # This exercises identity normalization, not the rolling 30-day expiry.
    updated_at = datetime.now(UTC)
    fetched_at = (updated_at - timedelta(hours=1)).isoformat()
    refreshed_at = updated_at.isoformat()
    repository.save_valorant_cache(
        "ExamplePlayer#12345",
        {"version": 1, "rank": "黄金 1", "matches": [{"acs": 208}]},
        fetched_at=fetched_at,
    )

    cached = repository.get_valorant_cache("ExamplePlayer＃60450")
    assert cached is not None
    assert cached.riot_id == "ExamplePlayer#12345"
    assert cached.payload["rank"] == "黄金 1"
    assert cached.payload["matches"] == [{"acs": 208}]
    assert cached.fetched_at == fetched_at

    repository.save_valorant_cache(
        "ExamplePlayer＃60450",
        {"version": 1, "rank": "黄金 2", "matches": []},
        fetched_at=refreshed_at,
    )
    updated = repository.get_valorant_cache("ExamplePlayer#12345")
    assert updated is not None
    assert updated.riot_id == "ExamplePlayer＃60450"
    assert updated.payload["rank"] == "黄金 2"
    assert updated.fetched_at == refreshed_at


def test_valorant_cache_discards_entries_older_than_thirty_days(tmp_path: Path) -> None:
    repository = BindingRepository(tmp_path / "bindings.db")
    repository.save_valorant_cache(
        "expired#10001", {"version": 1},
        fetched_at=(datetime.now(UTC) - timedelta(days=31)).isoformat(),
    )
    assert repository.get_valorant_cache("expired#10001") is None


def test_remove_members_not_in_group_keeps_only_verified_members(tmp_path: Path) -> None:
    repository = BindingRepository(tmp_path / "bindings.db")
    repository.bind("group", "qq-1", "76561198000000001")
    repository.bind("group", "qq-2", "76561198000000002")
    repository.bind("other", "qq-2", "76561198000000003")
    repository.initialize_match_tracking("group", "76561198000000002", ["old"])
    repository.add_group_ai_memory(
        "group",
        "在群成员的记忆",
        created_by_qq="qq-1",
        created_by_nickname="One",
    )
    repository.add_group_ai_memory(
        "group",
        "退群成员的记忆",
        created_by_qq="qq-2",
        created_by_nickname="Two",
    )
    repository.add_group_ai_memory(
        "other",
        "其他群不受影响",
        created_by_qq="qq-2",
        created_by_nickname="Two",
    )
    repository.reconcile_group_member_compliance(
        "group",
        {
            "100001": ("level",),
            "100002": ("unbound",),
        },
        check_date="2026-07-01",
    )

    removed = repository.remove_members_not_in_group(
        "group",
        {"qq-1", "qq-3", "100001"},
    )

    assert [(item.qq_id, item.steam_id) for item in removed] == [
        ("qq-2", "76561198000000002")
    ]
    assert repository.get_by_qq("group", "qq-1") is not None
    assert repository.get_by_qq("group", "qq-2") is None
    assert repository.get_by_qq("other", "qq-2") is not None
    assert not repository.is_match_notified("group", "76561198000000002", "old")
    assert repository.get_group_member_compliance("group", "100001") is not None
    assert repository.get_group_member_compliance("group", "100002") is None
    assert [
        entry.created_by_qq for entry in repository.list_group_ai_memories("group")
    ] == ["qq-1"]
    assert len(repository.list_group_ai_memories("other")) == 1


def test_member_reconciliation_cleans_party_and_commentary_preferences(
    tmp_path: Path,
) -> None:
    repository = BindingRepository(tmp_path / "bindings.db")
    repository.create_party(
        "group",
        "100001",
        "车主",
        capacity=5,
        scheduled_for="22:30",
    )
    repository.join_party("group", "100002", "队友")
    repository.set_match_commentary_enabled("group", "100001", enabled=False)

    repository.remove_members_not_in_group("group", {"100002"})

    party = repository.get_party("group")
    assert party is not None
    assert party.owner_qq == "100002"
    assert [member.qq_id for member in party.members] == ["100002"]
    assert repository.is_match_commentary_enabled("group", "100001") is True


def test_legacy_single_party_schema_migrates_to_numbered_multi_party(
    tmp_path: Path,
) -> None:
    path = tmp_path / "bindings.db"
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TABLE game_parties (
                group_id TEXT PRIMARY KEY,
                owner_qq TEXT NOT NULL,
                capacity INTEGER NOT NULL,
                scheduled_for TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE game_party_members (
                group_id TEXT NOT NULL,
                qq_id TEXT NOT NULL,
                nickname TEXT NOT NULL,
                joined_at TEXT NOT NULL,
                PRIMARY KEY (group_id, qq_id),
                FOREIGN KEY (group_id) REFERENCES game_parties(group_id)
                    ON DELETE CASCADE
            )
            """
        )
        connection.execute(
            """
            INSERT INTO game_parties VALUES
                ('group', '100001', 5, '22:30', '2026-07-01', '2026-07-01')
            """
        )
        connection.execute(
            """
            INSERT INTO game_party_members VALUES
                ('group', '100001', '旧车主', '2026-07-01')
            """
        )

    repository = BindingRepository(path)
    migrated = repository.list_parties("group")
    second = repository.create_party(
        "group",
        "100002",
        "新车主",
        name="第二辆车",
        capacity=3,
        scheduled_for="",
    )

    assert len(migrated) == 1
    assert migrated[0].party_id > 0
    assert migrated[0].name == "默认车队"
    assert [member.nickname for member in migrated[0].members] == ["旧车主"]
    assert second.party_id != migrated[0].party_id
    assert len(repository.list_parties("group")) == 2
    assert len(BindingRepository(path).list_parties("group")) == 2


def test_group_member_compliance_persists_reasons_and_resets(tmp_path: Path) -> None:
    path = tmp_path / "bindings.db"
    repository = BindingRepository(path)

    first = repository.reconcile_group_member_compliance(
        "group",
        {"100001": ("unbound",)},
        check_date="2026-07-01",
    )
    assert len(first) == 1
    assert first[0].first_noncompliant_date == "2026-07-01"
    assert first[0].last_checked_date == "2026-07-01"
    assert first[0].reasons == ("unbound",)

    reopened = BindingRepository(path)
    updated = reopened.reconcile_group_member_compliance(
        "group",
        {"100001": ("level",)},
        check_date="2026-07-02",
    )
    assert len(updated) == 1
    assert updated[0].first_noncompliant_date == "2026-07-01"
    assert updated[0].last_checked_date == "2026-07-02"
    assert updated[0].reasons == ("level",)

    after_gap = reopened.reconcile_group_member_compliance(
        "group",
        {"100001": ("unbound",)},
        check_date="2026-07-04",
    )
    assert after_gap[0].first_noncompliant_date == "2026-07-04"
    assert after_gap[0].last_checked_date == "2026-07-04"

    assert reopened.reconcile_group_member_compliance(
        "group",
        {},
        check_date="2026-07-05",
    ) == ()
    assert reopened.get_group_member_compliance("group", "100001") is None

    restarted = reopened.reconcile_group_member_compliance(
        "group",
        {"100001": ("unbound",)},
        check_date="2026-07-06",
    )
    assert restarted[0].first_noncompliant_date == "2026-07-06"


def test_unbound_message_usage_is_persistent_and_clearable(tmp_path: Path) -> None:
    path = tmp_path / "bindings.db"
    repository = BindingRepository(path)

    assert repository.increment_unbound_message_count("group", "qq", "2026-07-02") == 1
    assert repository.increment_unbound_message_count("group", "qq", "2026-07-02") == 2
    reopened = BindingRepository(path)
    assert reopened.get_unbound_message_count("group", "qq", "2026-07-02") == 2

    reopened.clear_unbound_message_usage("group", "qq")
    assert reopened.get_unbound_message_count("group", "qq", "2026-07-02") == 0


def test_admin_ban_guard_audit_and_conditional_daily_admin_clear(
    tmp_path: Path,
) -> None:
    repository = BindingRepository(tmp_path / "bindings.db")
    repository.complete_daily_admin_assignment("group", "2026-07-23", "100001")

    assert repository.clear_daily_admin_if("group", "100002") is False
    assert repository.get_daily_admin("group") == "100001"
    assert repository.clear_daily_admin_if("group", "100001") is True
    assert repository.get_daily_admin("group") is None

    repository.record_admin_ban_guard_event(
        "group",
        "100001",
        "100002",
        601,
        unmuted=True,
        admin_revoked=True,
    )
    event = repository.get_latest_admin_ban_guard_event("group")
    assert event is not None
    assert event.operator_qq_id == "100001"
    assert event.target_qq_id == "100002"
    assert event.duration_seconds == 601
    assert event.unmuted is True
    assert event.admin_revoked is True


def test_match_tracking_baseline_and_notification_deduplication(tmp_path: Path) -> None:
    repository = BindingRepository(tmp_path / "bindings.db")
    repository.bind("group-2", "qq-2", "76561198000000002")
    repository.bind("group-1", "qq-1", "76561198000000001")

    assert [binding.group_id for binding in repository.list_bindings()] == [
        "group-1",
        "group-2",
    ]
    assert [binding.steam_id for binding in repository.list_group_bindings("group-1")] == [
        "76561198000000001"
    ]
    assert repository.initialize_match_tracking(
        "group-1",
        "76561198000000001",
        ["old-match"],
    )
    assert not repository.initialize_match_tracking(
        "group-1",
        "76561198000000001",
        ["new-match"],
    )
    assert repository.is_match_notified(
        "group-1", "76561198000000001", "old-match"
    )
    assert repository.is_group_match_notified("group-1", "old-match")
    assert not repository.is_match_notified(
        "group-1", "76561198000000001", "new-match"
    )

    repository.mark_match_notified("group-1", "76561198000000001", "new-match")

    assert repository.is_match_notified(
        "group-1", "76561198000000001", "new-match"
    )
    assert repository.is_group_match_notified("group-1", "new-match")

    repository.unbind("group-1", "qq-1")
    assert repository.is_group_match_notified("group-1", "new-match")
    repository.bind("group-1", "qq-1", "76561198000000001")

    assert repository.initialize_match_tracking(
        "group-1", "76561198000000001", ["current-match"]
    )


def test_match_source_tracking_is_independent_and_preserves_legacy_source(
    tmp_path: Path,
) -> None:
    repository = BindingRepository(tmp_path / "bindings.db")
    group_id = "group"
    steam_id = "76561198000000001"
    shared_id = "shared-match-id"
    repository.bind(group_id, "qq", steam_id)

    assert repository.initialize_match_source_tracking(
        group_id,
        steam_id,
        3,
        [shared_id],
    )
    assert repository.match_tracking_initialized_at(group_id, steam_id)
    assert repository.is_group_match_notified(group_id, shared_id)

    assert repository.initialize_match_source_tracking(
        group_id,
        steam_id,
        1,
        [shared_id],
    )
    assert not repository.initialize_match_source_tracking(
        group_id,
        steam_id,
        1,
        ["new-official"],
    )
    assert repository.match_source_tracking_initialized_at(
        group_id,
        steam_id,
        1,
    )
    assert repository.is_source_match_notified(
        group_id,
        steam_id,
        1,
        shared_id,
    )
    assert repository.is_group_source_match_notified(
        group_id,
        1,
        shared_id,
    )

    repository.mark_source_match_notified(
        group_id,
        steam_id,
        1,
        "new-official",
    )
    assert repository.is_source_match_notified(
        group_id,
        steam_id,
        1,
        "new-official",
    )
    assert repository.is_group_source_match_notified(
        group_id,
        1,
        "new-official",
    )
    assert not repository.is_match_notified(
        group_id,
        steam_id,
        "new-official",
    )
