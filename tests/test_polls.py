from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier

import pytest

from pwcs2_bot.polls import PollService


class Clock:
    def __init__(self):
        self.now = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)

    def __call__(self):
        return self.now


@pytest.fixture
def polls(tmp_path):
    return PollService(tmp_path / "bindings.db", clock=Clock())


def create(polls, *, user="1", group="g", request_id="", duration=30, title="今晚玩什么？"):
    return polls.handle(group, user, "member",
                        f"发起 {duration}分钟 {title} | CS2 | 瓦", request_id)


def test_help_create_vote_change_results_and_persistence(polls):
    assert "暂无" in polls.handle("g", "1", "member", "")
    assert "不消耗牛币" in polls.handle("g", "1", "member", "帮助")
    assert "投票 #1" in polls.handle("g", "1", "member", "发起 今晚玩什么？ | CS2 | 瓦")
    assert "开放30分钟" in polls.handle("g", "2", "member", "发起 排图 | 米垃圾 | 沙二")
    assert "已投 A" in polls.handle("g", "3", "member", "1 a")
    assert "已改投 B" in polls.handle("g", "3", "member", "1 2")
    assert "已投 B" in polls.handle("g", "3", "member", "1 B")
    reloaded = PollService(polls.path, clock=polls.clock)
    result = reloaded.handle("g", "4", "member", "结果 1")
    assert "CS2：0票" in result and "瓦：1票" in result
    assert "共1人参与" in result
    assert "截止北京时间10-18 20:30" in result
    assert "#1 今晚玩什么？｜1票" in reloaded.handle("g", "4", "member", "列表")


def test_exact_expiry_stops_votes_and_frees_creator_and_group_slot(polls):
    create(polls, duration=1)
    create(polls, user="2")
    create(polls, user="3")
    polls.clock.now += timedelta(seconds=59, microseconds=999999)
    assert "已投" in polls.handle("g", "4", "member", "1 A")
    polls.clock.now += timedelta(microseconds=1)
    assert "已结束" in polls.handle("g", "5", "member", "1 B")
    result = polls.handle("g", "4", "member", "结果 1")
    assert "共1人参与" in result and "已到期" in result
    assert "#1 " not in polls.handle("g", "4", "member", "列表")
    assert "投票 #4" in create(polls)


@pytest.mark.parametrize("duration", [1, 1440])
def test_duration_edges_are_accepted(polls, duration):
    assert f"开放{duration}分钟" in create(polls, duration=duration)


@pytest.mark.parametrize("duration", [0, -1, 1441, 99999999999999999999])
def test_out_of_range_duration_is_rejected(polls, duration):
    assert "1—1440" in create(polls, duration=duration)
    assert "暂无" in polls.handle("g", "1", "member", "列表")


def test_two_to_six_unique_options(polls):
    assert "2—6" in polls.handle("g", "1", "member", "发起 问题 | 一")
    assert "2—6" in polls.handle("g", "1", "member", "发起 问题 | | 二")
    assert "不能重复" in polls.handle("g", "1", "member", "发起 问题 | CS2 | cs2")
    assert "2—6" in polls.handle("g", "1", "member", "发起 问题 | A | B | C | D | E | F | G")
    assert "F. 六" in polls.handle("g", "1", "member", "发起 1分钟 问题｜一｜二｜三｜四｜五｜六")
    assert "已投 F" in polls.handle("g", "2", "member", "1 6")


@pytest.mark.parametrize("argument,expected", [
    ("发起 " + "问" * 101 + " | A | B", "问题最多100"),
    ("发起 问题 | " + "A" * 41 + " | B", "选项最多40"),
    ("x" * 513, "命令最多512"),
    ("发起 问题 | [CQ:at,qq=all] | B", "CQ"),
    ("发起 问题 | [cQ:at,qq=all] | B", "CQ"),
    ("发起 问题\n伪造公告 | A | B", "控制符"),
    ("发起 问题 | A\x00 | B", "控制符"),
    ("发起 问题 | A\u202e | B", "隐藏字符"),
    ("发起 问题 | A\u200b | B", "隐藏字符"),
])
def test_plain_text_safety_and_length_limits(polls, argument, expected):
    assert expected in polls.handle("g", "1", "member", argument)
    assert "暂无" in polls.handle("g", "1", "member", "列表")


@pytest.mark.parametrize("choice", ["0", "7", "Z", "AA", "1 2", "-1"])
def test_invalid_vote_does_not_count(polls, choice):
    create(polls)
    assert "请选择" in polls.handle("g", "2", "member", f"1 {choice}")
    assert "共0人参与" in polls.handle("g", "2", "member", "结果 1")


def test_nonexistent_options_and_numbers(polls):
    create(polls)
    assert "没有该选项" in polls.handle("g", "2", "member", "1 F")
    assert "没有这个投票" in polls.handle("g", "2", "member", "结果 999999999999")
    assert "有效的投票编号" in polls.handle("g", "2", "member", "结果 -1")
    assert "有效的投票编号" in polls.handle("g", "2", "member", "结果 " + "9" * 100)


@pytest.mark.parametrize("role", ["admin", "owner"])
def test_admins_can_end_and_cannot_reopen(polls, role):
    create(polls)
    assert "已结束" in polls.handle("g", "2", role, "结束 1")
    assert "已结束" in polls.handle("g", "1", "member", "1 A")
    assert "提前结束" in polls.handle("g", "2", role, "结束 1")
    assert "投票 #2" in create(polls)


def test_creator_end_and_unauthorized_end(polls):
    create(polls)
    assert "只有投票发起者" in polls.handle("g", "2", "member", "结束 1")
    assert "进行中" in polls.handle("g", "2", "member", "结果 1")
    assert "已结束" in polls.handle("g", "1", "member", "结束 1")


def test_all_access_is_group_isolated(polls):
    create(polls)
    assert "暂无" in polls.handle("other", "1", "owner", "列表")
    for action in ("1 A", "结果 1", "结束 1"):
        assert "本群没有这个投票" in polls.handle("other", "1", "owner", action)
    assert "投票 #2" in create(polls, group="other")
    assert "共0人参与" in polls.handle("g", "1", "member", "结果 1")


def test_creator_limit_and_group_limit(polls):
    create(polls)
    assert "你已有" in create(polls)
    create(polls, user="2")
    create(polls, user="3")
    assert "本群已有3个" in create(polls, user="4")


def test_request_replay_persists_and_does_not_reopen_after_expiry(polls):
    original = create(polls, request_id="create")
    polls.clock.now += timedelta(days=2)
    reloaded = PollService(polls.path, clock=polls.clock)
    assert create(reloaded, request_id="create") == original
    assert "暂无" in reloaded.handle("g", "1", "member", "列表")
    assert "同一条消息" in reloaded.handle("g", "1", "member", "结束 1", "create")
    assert "投票 #2" in create(reloaded, request_id="new-create")


def test_vote_replay_does_not_overwrite_a_later_changed_vote(polls):
    create(polls)
    first = polls.handle("g", "2", "member", "1 A", "first-vote")
    polls.handle("g", "2", "member", "1 B", "changed-vote")
    assert polls.handle("g", "2", "member", "1 A", "first-vote") == first
    assert "CS2：0票" in polls.handle("g", "1", "member", "结果 1")
    assert "瓦：1票" in polls.handle("g", "1", "member", "结果 1")


def test_request_ids_are_scoped_to_group_and_member(polls):
    create(polls, request_id="same")
    assert "投票 #2" in create(polls, user="2", request_id="same")
    assert "投票 #3" in create(polls, group="other", request_id="same")


def test_failed_actions_do_not_consume_request_id(polls):
    create(polls)
    assert "只有投票发起者" in polls.handle("g", "2", "member", "结束 1", "retry")
    assert "已结束" in polls.handle("g", "2", "admin", "结束 1", "retry")


def test_concurrent_creates_obey_group_limit(tmp_path):
    path = tmp_path / "polls.db"
    services = [PollService(path) for _ in range(8)]
    barrier = Barrier(8)

    def run(index):
        barrier.wait(timeout=10)
        return create(services[index], user=str(index), request_id=f"message-{index}")

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(run, range(8)))
    assert sum("🗳 投票" in result for result in results) == 3
    assert sum("本群已有3个" in result for result in results) == 5


def test_concurrent_same_request_creates_once_and_replies_consistently(tmp_path):
    path = tmp_path / "polls.db"
    services = [PollService(path) for _ in range(6)]
    barrier = Barrier(6)

    def run(index):
        barrier.wait(timeout=10)
        return create(services[index], request_id="same-event")

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(run, range(6)))
    assert len(set(results)) == 1
    listing = services[0].handle("g", "1", "member", "列表")
    assert listing.count("今晚玩什么？") == 1


def test_concurrent_votes_are_one_per_member_and_persist(tmp_path):
    path = tmp_path / "polls.db"
    services = [PollService(path) for _ in range(8)]
    create(services[0])
    barrier = Barrier(8)

    def run(index):
        barrier.wait(timeout=10)
        return services[index].handle("g", str(index % 4), "member", f"1 {index % 2 + 1}")

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(run, range(8)))
    assert all("已投" in result for result in results)
    assert "共4人参与" in PollService(path).handle("g", "1", "member", "结果 1")


def test_naive_clock_is_a_programming_error_not_local_timezone_assumption(tmp_path):
    service = PollService(tmp_path / "polls.db", clock=lambda: datetime(2026, 10, 18))
    with pytest.raises(RuntimeError, match="timezone-aware"):
        service.handle("g", "1", "member", "列表")


def test_existing_tables_remain_untouched(tmp_path: Path):
    import sqlite3

    path = tmp_path / "bindings.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE existing_bindings (qq_id TEXT, steam_id TEXT)")
        conn.execute("INSERT INTO existing_bindings VALUES ('1', '76561190000000001')")
    service = PollService(path)
    create(service)
    PollService(path)
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT * FROM existing_bindings").fetchall() == [
            ("1", "76561190000000001")
        ]
