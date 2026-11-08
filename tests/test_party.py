import re
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from pwcs2_bot.database import BindingRepository
from pwcs2_bot.party import PartyService


def _service(tmp_path: Path) -> PartyService:
    repository = BindingRepository(tmp_path / "bindings.db")
    repository.upsert_group_member_identity("group", "10001", "车主")
    repository.upsert_group_member_identity("group", "10002", "突破手")
    repository.upsert_group_member_identity("group", "10003", "狙击手")
    return PartyService(
        repository,
        teamspeak_address="ts.example.com",
    )


def test_party_create_join_fill_and_persist(tmp_path: Path) -> None:
    service = _service(tmp_path)

    created = service.create("group", "10001", "3 22:30")
    second = service.join("group", "10002")
    full = service.join("group", "10003")

    assert "1/3" in created
    assert "开打时间：22:30" in created
    assert "突破手" in second
    assert "人齐了" in full
    assert "ts.example.com" in full
    assert "3/3" in service.status("group")
    with pytest.raises(ValueError, match="满员"):
        service.repository.join_party("group", "10004", "替补")


def test_party_owner_transfers_and_only_owner_can_disband(tmp_path: Path) -> None:
    service = _service(tmp_path)
    service.create("group", "10001", "5")
    service.join("group", "10002")

    with pytest.raises(ValueError, match="车主"):
        service.disband("group", "10002")
    after_leave = service.leave("group", "10001")

    assert "突破手（车主）" in after_leave
    assert "已解散" in service.disband("group", "10002")
    assert "没有车队" in service.status("group")


@pytest.mark.parametrize(
    "argument",
    ["1", "11", "6v6", "5 25:00", "5人 25:00", "5 6 22:00", "晚上八点 21:00"],
)
def test_party_rejects_invalid_create_arguments(tmp_path: Path, argument: str) -> None:
    service = _service(tmp_path)
    with pytest.raises(ValueError):
        service.create("group", "10001", argument)


@pytest.mark.parametrize(
    ("argument", "expected"),
    [
        ("晚上八点荒漠迷城五人车", (5, "20:00", "荒漠迷城")),
        ("牛牛快乐车 4个 明晚九点半", (4, "明天 21:30", "牛牛快乐车")),
        ("5 22:30", (5, "22:30", "")),
        ("车名叫 天梯冲分 3人 10点", (3, "22:00", "天梯冲分")),
        ("早上八点 双排 2人", (2, "08:00", "双排")),
        ("五黑今晚九点", (5, "21:00", "")),
        ("内战 5v5 晚上八点", (10, "20:00", "内战")),
        ("内战 5VS5 10人", (10, "", "内战")),
        ("练枪 1v1", (2, "", "练枪")),
    ],
)
def test_party_fuzzy_create_argument_parsing(
    argument: str,
    expected: tuple[int, str, str],
) -> None:
    assert PartyService._parse_create_argument(argument) == expected


def test_group_can_have_multiple_named_parties_and_fuzzy_select_them(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)

    service.create("group", "10001", "晚上八点荒漠迷城五人车")
    service.create("group", "10002", "天梯冲分 3人 21:30")
    parties = service.repository.list_parties("group")

    assert [party.name for party in parties] == ["荒漠迷城", "天梯冲分"]
    assert "本群当前有 2 辆车" in service.status("group")
    with pytest.raises(ValueError, match="多辆车"):
        service.join("group", "10003")

    assert "荒漠迷城" in service.join("group", "10003", "荒漠")
    assert "天梯冲分" in service.join("group", "10003", f"#{parties[1].party_id}")
    with pytest.raises(ValueError, match="多辆车"):
        service.leave("group", "10003")

    assert "荒漠迷城" in service.leave("group", "10003", str(parties[0].party_id))
    assert "天梯冲分" in service.status("group", "天梯")
    assert "荒漠迷城" in service.disband("group", "10001", "荒漠")
    assert len(service.repository.list_parties("group")) == 1


def test_fuzzy_selector_requires_id_when_multiple_names_match(tmp_path: Path) -> None:
    service = _service(tmp_path)
    service.create("group", "10001", "天梯一队 5人")
    service.create("group", "10002", "天梯二队 5人")

    with pytest.raises(ValueError, match="匹配到多辆车"):
        service.status("group", "天梯")


def _populated_party(service: PartyService, count: int, *, group: str = "group") -> int:
    service.create(group, "10001", f"内战车 {max(2, count)}人")
    for number in range(2, count + 1):
        service.join(group, f"100{number:02d}")
    return service.repository.list_parties(group)[0].party_id


def _remember_score(
    service: PartyService,
    qq_id: str,
    score: object,
    *,
    source: str = "perfect",
    ended_at: str | None = None,
    group: str = "group",
) -> None:
    service.repository.bind(group, qq_id, str(76561198000000000 + int(qq_id)))
    service.repository.update_group_member_stats(
        group, qq_id,
        {"last_match": {
            "source": source,
            "elo": score,
            "ended_at": ended_at or (datetime.now(UTC) - timedelta(hours=1)).isoformat(),
        }},
    )


@pytest.mark.parametrize("count", [2, 4, 6, 8, 10])
def test_balanced_party_has_equal_teams_without_changing_seats(
    tmp_path: Path, count: int,
) -> None:
    service = _service(tmp_path)
    party_id = _populated_party(service, count)
    before = service.repository.get_party_by_id("group", party_id)

    result = service.balance("group", "10002")

    assert "稳定随机均分" in result
    assert f"{count // 2} v {count // 2}" in result
    for label in ("A 队：", "B 队："):
        line = next(line for line in result.splitlines() if line.startswith(label))
        assert len(re.findall(r"（QQ (\d+)）", line)) == count // 2
    assert sorted(re.findall(r"（QQ (\d+)）", result)) == [
        f"100{number:02d}" for number in range(1, count + 1)
    ]
    assert service.repository.get_party_by_id("group", party_id) == before
    assert result == service.balance("group", "10001", str(party_id))
    reopened = PartyService(
        BindingRepository(service.repository.path), teamspeak_address="",
    )
    assert result == reopened.balance("group", "10001", "内战")


@pytest.mark.parametrize("count", [1, 3, 5, 7, 9])
def test_balance_requires_even_actual_members(tmp_path: Path, count: int) -> None:
    service = _service(tmp_path)
    _populated_party(service, count)
    with pytest.raises(ValueError, match="偶数"):
        service.balance("group", "10001")


def test_balance_checks_membership_and_group_scope(tmp_path: Path) -> None:
    service = _service(tmp_path)
    party_id = _populated_party(service, 2)
    with pytest.raises(ValueError, match="没有上车"):
        service.balance("group", "10003")
    with pytest.raises(ValueError, match="不在这辆车"):
        service.balance("group", "10003", str(party_id))
    with pytest.raises(ValueError, match="没有车队"):
        service.balance("other", "10001", str(party_id))
    _populated_party(service, 2, group="other")
    with pytest.raises(ValueError, match="没有找到编号"):
        service.balance("other", "10001", str(party_id))


def test_balance_duplicate_join_does_not_duplicate_participant(tmp_path: Path) -> None:
    service = _service(tmp_path)
    _populated_party(service, 2)
    service.join("group", "10002")
    result = service.balance("group", "10002")
    assert result.count("（QQ 10002）") == 1


def test_balance_rejects_corrupt_duplicate_members(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service(tmp_path)
    _populated_party(service, 2)
    party = service.repository.list_parties("group")[0]
    corrupt = replace(party, members=(party.members[0], party.members[0]))
    monkeypatch.setattr(service.repository, "list_parties", lambda _group: (corrupt,))
    with pytest.raises(ValueError, match="重复"):
        service.balance("group", "10001")


def test_balance_minimizes_total_perfect_score_and_is_stable(tmp_path: Path) -> None:
    service = _service(tmp_path)
    _populated_party(service, 4)
    for number, score in enumerate((1000, 1400, 1600, 2000), start=1):
        _remember_score(service, f"100{number:02d}", score)

    result = service.balance("group", "10001")

    assert result.count("参考总分 3000") == 2
    assert "参考总分差：0" in result
    assert "估算" not in result
    assert "完美对局分数缓存" in result
    assert result.count("对局缓存 ") == 4
    assert "北京时间" in result
    assert result == service.balance("group", "10004")


def test_balance_estimates_missing_and_stale_scores_using_median(tmp_path: Path) -> None:
    service = _service(tmp_path)
    _populated_party(service, 4)
    _remember_score(service, "10001", 1200)
    _remember_score(service, "10002", 1800)
    _remember_score(
        service, "10003", 5000,
        ended_at=(datetime.now(UTC) - timedelta(days=8)).isoformat(),
    )
    result = service.balance("group", "10001")
    assert "中位数 1500" in result
    assert result.count("1500*（估算）") == 2
    assert "5000" not in result
    assert "参考总分差：0" in result


@pytest.mark.parametrize("score", [0, -1, 10001, True, "NaN", "inf", None, "未知"])
def test_balance_does_not_trust_invalid_score(tmp_path: Path, score: object) -> None:
    service = _service(tmp_path)
    _populated_party(service, 2)
    _remember_score(service, "10001", score)
    assert "无可信完美分数缓存" in service.balance("group", "10001")


@pytest.mark.parametrize("ended_at", ["未知时间", "2099-01-01T00:00:00+00:00"])
def test_balance_does_not_trust_invalid_or_future_match_time(
    tmp_path: Path, ended_at: str,
) -> None:
    service = _service(tmp_path)
    _populated_party(service, 2)
    _remember_score(service, "10001", 2500, ended_at=ended_at)
    assert "无可信完美分数缓存" in service.balance("group", "10001")


def test_balance_does_not_use_official_season_only_or_other_group_data(tmp_path: Path) -> None:
    service = _service(tmp_path)
    _populated_party(service, 2)
    _remember_score(service, "10001", 2000, source="official")
    service.repository.update_group_member_stats(
        "group", "10002", {"season_stats": {"source": "perfect", "elo": 2100}},
    )
    _remember_score(service, "10002", 3000, group="other")
    assert "稳定随机均分" in service.balance("group", "10001")


def test_balance_does_not_refresh_old_score_age_from_other_stats(tmp_path: Path) -> None:
    service = _service(tmp_path)
    _populated_party(service, 2)
    _remember_score(
        service, "10001", 2400,
        ended_at=(datetime.now(UTC) - timedelta(days=20)).isoformat(),
    )
    service.repository.update_group_member_stats(
        "group", "10001", {"official_stats": {"rating": 1.5}},
    )
    assert "无可信完美分数缓存" in service.balance("group", "10001")


def test_balance_does_not_use_score_after_member_changes_binding(tmp_path: Path) -> None:
    service = _service(tmp_path)
    _populated_party(service, 2)
    _remember_score(service, "10001", 2400)
    service.repository.bind("group", "10001", "76561198000000099")
    assert "无可信完美分数缓存" in service.balance("group", "10001")
