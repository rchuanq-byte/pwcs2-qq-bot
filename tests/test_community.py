from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from pwcs2_bot.community import (
    LEGACY_GRENADE_QUESTIONS,
    LEGACY_PRO_PLAYER_QUESTIONS_V1,
    PRO_PLAYER_QUESTIONS,
    CommunityError,
    CommunityRepository,
    GroupPlayService,
    HistorySource,
    QuizReveal,
    format_quiz_question,
    format_quiz_reveal,
    question_by_key,
    question_for,
)
from pwcs2_bot.database import BindingRepository


def _services(tmp_path: Path) -> tuple[CommunityRepository, GroupPlayService]:
    path = tmp_path / "bindings.db"
    bindings = BindingRepository(path)
    community = CommunityRepository(path)
    return community, GroupPlayService(
        community,
        bindings,
        prediction_duration_minutes=30,
    )


def test_wallet_shop_and_inventory_are_persistent(tmp_path: Path) -> None:
    repository, service = _services(tmp_path)

    assert "100" in service.wallet("group", "10001")
    assert "称号" in service.buy("group", "10001", "1")
    balance, badge = repository.wallet("group", "10001")

    assert balance == 20
    assert badge


def test_equipped_badge_lookup_is_read_only_and_group_scoped(tmp_path: Path) -> None:
    repository, service = _services(tmp_path)

    assert repository.equipped_badge("group", "10001") == ""
    with repository._connect() as connection:
        wallet_count = connection.execute("SELECT COUNT(*) FROM coin_wallets").fetchone()[0]
    assert wallet_count == 0

    repository.add_coins(
        "group", "10001", 300, reason="test", reference_key="badge-read-credit"
    )
    service.buy("group", "10001", "5")
    service.use_custom_badge("group", "10001", "残局指挥官")

    assert repository.equipped_badge("group", "10001") == "残局指挥官"
    assert service.equipped_badge("group", "10001") == "残局指挥官"
    assert repository.equipped_badge("other-group", "10001") == ""


def test_custom_badge_hint_and_shop_balance(tmp_path: Path) -> None:
    repository, service = _services(tmp_path)
    repository.add_coins(
        "group", "10001", 300, reason="test", reference_key="badge-credit"
    )

    assert "余额 400" in service.shop("group", "10001")
    assert "自选称号券" in service.buy("group", "10001", "5")
    assert "吊大指挥官" in service.use_custom_badge("group", "10001", "吊大指挥官")
    assert repository.wallet("group", "10001")[1] == "吊大指挥官"
    assert repository.inventory("group", "10001").get("custom_badge", 0) == 0

    quiz_date = "2026-07-26"
    question = question_for("group", quiz_date)
    repository.ensure_quiz_run("group", quiz_date, question.key)
    repository.mark_quiz_question_sent("group", quiz_date)
    assert "答题提示卡" in service.buy("group", "10001", "6")
    hint = service.quiz_hint("group", "10001", quiz_date=quiz_date)
    assert "答案只可能是以下两项" in hint
    assert f"{question.answer}. {question.options[ord(question.answer) - ord('A')]}" in hint
    assert service.quiz_hint("group", "10001", quiz_date=quiz_date) == hint
    assert repository.inventory("group", "10001").get("quiz_hint", 0) == 0


def test_tip_burns_fee_and_records_ledger(tmp_path: Path) -> None:
    repository, service = _services(tmp_path)
    service.bindings.upsert_group_member_identity("group", "10001", "牛牛")
    service.bindings.upsert_group_member_identity("group", "10002", "精灵")

    result = service.tip("group", "10001", "@10002 50")

    assert "精灵 45 牛币" in result
    assert "手续费 5" in result
    assert repository.wallet("group", "10001")[0] == 50
    assert repository.wallet("group", "10002")[0] == 145
    ledger = service.coin_ledger("group", "10001")
    assert "-50" in ledger
    assert "打赏 QQ 10002" in ledger


def test_daily_checkin_awards_streak_bonus_once(tmp_path: Path) -> None:
    repository, service = _services(tmp_path)

    for day in range(20, 27):
        result = service.checkin(
            "group", "10001", checkin_date=f"2026-07-{day:02d}"
        )
        assert f"连续 {day - 19} 天" in result

    assert "连续 7 天奖励 +15" in result
    assert repository.wallet("group", "10001")[0] == 150

    duplicate = service.checkin("group", "10001", checkin_date="2026-07-26")
    assert "今天已经签到过了" in duplicate
    assert repository.wallet("group", "10001")[0] == 150

    reset = service.checkin("group", "10001", checkin_date="2026-07-28")
    assert "连续 1 天" in reset
    assert repository.wallet("group", "10001")[0] == 155


def test_completed_match_rewards_daily_cap_and_first_win(tmp_path: Path) -> None:
    repository, _service = _services(tmp_path)
    rewards = [
        repository.reward_completed_match(
            "group",
            "10001",
            f"match-{index}",
            "2026-07-26",
            won=index == 4,
        )
        for index in range(1, 6)
    ]

    assert [reward.base_reward for reward in rewards if reward is not None] == [
        5,
        5,
        5,
        0,
        0,
    ]
    assert [reward.win_bonus for reward in rewards if reward is not None] == [
        0,
        0,
        0,
        5,
        0,
    ]
    assert repository.wallet("group", "10001")[0] == 120
    assert (
        repository.reward_completed_match(
            "group", "10001", "match-4", "2026-07-26", won=True
        )
        is None
    )
    assert repository.wallet("group", "10001")[0] == 120


def test_prediction_insurance_refunds_half_with_cap(tmp_path: Path) -> None:
    repository, service = _services(tmp_path)
    service.bindings.upsert_group_member_identity("group", "10001", "赢家")
    service.bindings.upsert_group_member_identity("group", "10002", "输家")
    assert "竞猜保险" in service.buy("group", "10002", "7")
    service.create_prediction("group", "10001", "admin", "谁赢 | A | B")
    service.bet("group", "10001", "1 20")
    service.bet("group", "10002", "2 20")

    result = service.settle_prediction("group", "10001", "owner", "1")

    assert "竞猜保险理赔" in result
    assert "输家 +10" in result
    assert repository.wallet("group", "10002")[0] == 10
    assert repository.inventory("group", "10002").get("prediction_insurance", 0) == 0
    assert repository.prediction_insurance_refunds(1) == [("10002", 10)]


def test_stats_backgrounds_are_permanent_and_switchable(tmp_path: Path) -> None:
    repository, service = _services(tmp_path)
    repository.add_coins(
        "group", "10001", 500, reason="test", reference_key="background-credit"
    )

    listing = service.stats_background("group", "10001", "")
    assert "当前：经典黑金" in listing
    assert "赛博霓虹" in listing
    assert "沙城余晖" in listing

    result = service.buy("group", "10001", "8")
    assert "永久解锁并切换为「赛博霓虹」" in result
    assert repository.stats_background("group", "10001") == "cyber_neon"
    balance = repository.wallet("group", "10001")[0]

    with pytest.raises(CommunityError, match="已经永久拥有"):
        service.buy("group", "10001", "8")
    assert repository.wallet("group", "10001")[0] == balance

    assert "经典黑金" in service.stats_background("group", "10001", "0")
    assert repository.stats_background("group", "10001") == "classic"
    assert "赛博霓虹" in service.stats_background("group", "10001", "霓虹")
    assert repository.stats_background("group", "10001") == "cyber_neon"
    with pytest.raises(CommunityError, match="请先发送 #购买 9"):
        service.stats_background("group", "10001", "沙城")


def test_admin_prediction_distributes_the_entire_pool(tmp_path: Path) -> None:
    repository, service = _services(tmp_path)
    with pytest.raises(CommunityError, match="管理员"):
        service.create_prediction("group", "10001", "member", "谁赢 | A | B")

    service.create_prediction("group", "10001", "admin", "谁赢 | A | B")
    assert "余额 70" in service.bet("group", "10001", "1 30")
    assert "余额 30" in service.bet("group", "10002", "2 70")
    result = service.settle_prediction("group", "10001", "owner", "1")

    assert "奖池 100" in result
    assert repository.wallet("group", "10001")[0] == 170
    assert repository.wallet("group", "10002")[0] == 30
    assert repository.active_prediction("group") is None


def test_predictions_can_be_fully_disabled(tmp_path: Path) -> None:
    path = tmp_path / "bindings.db"
    bindings = BindingRepository(path)
    repository = CommunityRepository(path)
    service = GroupPlayService(
        repository,
        bindings,
        prediction_enabled=False,
    )

    assert "竞猜保险" not in service.shop("group", "10001")
    assert "竞猜" not in service.coin_earning_guide()
    actions = (
        lambda: service.prediction_status("group"),
        lambda: service.create_prediction(
            "group", "10001", "owner", "谁赢 | A | B"
        ),
        lambda: service.bet("group", "10001", "1 10"),
        lambda: service.settle_prediction("group", "10001", "owner", "1"),
        lambda: service.cancel_prediction("group", "10001", "owner"),
        lambda: service.buy("group", "10001", "7"),
    )
    for action in actions:
        with pytest.raises(CommunityError, match="竞猜功能已关闭"):
            action()

    assert repository.wallet("group", "10001")[0] == 100


def test_disabling_predictions_refunds_any_open_bets(tmp_path: Path) -> None:
    repository, service = _services(tmp_path)
    service.create_prediction("group", "10001", "owner", "谁赢 | A | B")
    service.bet("group", "10001", "1 20")
    service.bet("group", "10002", "2 30")

    assert repository.cancel_all_open_predictions() == (1, 2)
    assert repository.wallet("group", "10001")[0] == 100
    assert repository.wallet("group", "10002")[0] == 100
    assert repository.active_prediction("group") is None
    assert repository.cancel_all_open_predictions() == (0, 0)


def test_disabling_predictions_retires_pending_auto_prediction(tmp_path: Path) -> None:
    repository, _service = _services(tmp_path)
    prediction, _run = repository.create_auto_prediction(
        "group",
        "live:watch:76561197960266101:PVP@123",
        "10001",
        "76561197960266101",
        "牛牛",
        "baseline",
        1,
        "win",
        None,
        "牛牛能否获胜？",
        ("能", "不能"),
        duration_minutes=30,
        expires_at=(datetime.now(UTC) + timedelta(hours=4)).isoformat(),
    )
    repository.place_bet("group", "10002", 1, 40)

    assert repository.cancel_all_open_predictions() == (1, 1)
    run = repository.auto_prediction_by_id(prediction.prediction_id)
    assert run is not None
    assert run.status == "void"
    assert run.result_sent
    assert repository.pending_auto_announcements() == ()
    assert repository.pending_auto_results() == ()
    assert repository.wallet("group", "10002")[0] == 100


def test_configured_manager_can_manage_predictions_as_group_member(tmp_path: Path) -> None:
    path = tmp_path / "bindings.db"
    bindings = BindingRepository(path)
    community = CommunityRepository(path)
    service = GroupPlayService(
        community,
        bindings,
        manager_qq_ids=["100000011"],
    )

    service.create_prediction("group", "100000011", "member", "谁赢 | A | B")
    result = service.cancel_prediction("group", "100000011", "member")

    assert "已取消" in result


def test_prediction_cannot_accept_bets_after_deadline(tmp_path: Path) -> None:
    repository, service = _services(tmp_path)
    service.create_prediction("group", "10001", "admin", "谁赢 | A | B")
    with repository._connect() as connection:
        connection.execute(
            "UPDATE predictions SET closes_at = ? WHERE group_id = ?",
            ((datetime.now(UTC) - timedelta(minutes=1)).isoformat(), "group"),
        )

    with pytest.raises(CommunityError, match="截止"):
        service.bet("group", "10002", "1 10")


def test_quiz_awards_fastest_and_consumes_double_card(tmp_path: Path) -> None:
    repository, _service = _services(tmp_path)
    quiz_date = "2026-07-22"
    question = question_for("group", quiz_date)
    repository.ensure_quiz_run("group", quiz_date, question.key)
    repository.mark_quiz_question_sent("group", quiz_date)
    repository.add_coins(
        "group", "10001", 100, reason="test", reference_key="test-credit"
    )
    repository.purchase("group", "10001", "2")
    repository.submit_quiz_answer(
        "group", quiz_date, "10001", "牛牛", question.answer
    )
    repository.submit_quiz_answer(
        "group", quiz_date, "10002", "精灵", question.answer
    )

    reveal = repository.reveal_quiz(
        "group",
        quiz_date,
        question,
        correct_reward=20,
        fastest_bonus=10,
    )

    assert reveal.fastest_name == "牛牛"
    assert reveal.rewards == (("10001", 60), ("10002", 20))
    assert repository.inventory("group", "10001").get("double_quiz", 0) == 0


def test_daily_quiz_uses_curated_pro_player_bank_and_keeps_legacy_reveals() -> None:
    question = question_for("group", "2026-07-24")

    assert question in PRO_PLAYER_QUESTIONS
    assert len(PRO_PLAYER_QUESTIONS) == 28
    assert len({item.key for item in PRO_PLAYER_QUESTIONS}) == len(PRO_PLAYER_QUESTIONS)
    assert len({item.prompt for item in PRO_PLAYER_QUESTIONS}) == len(
        PRO_PLAYER_QUESTIONS
    )
    assert all(len(item.options) == 4 for item in PRO_PLAYER_QUESTIONS)
    assert all(len(set(item.options)) == 4 for item in PRO_PLAYER_QUESTIONS)
    assert all(item.answer in {"A", "B", "C", "D"} for item in PRO_PLAYER_QUESTIONS)
    assert all(
        item.source_url.startswith("https://www.hltv.org/")
        for item in PRO_PLAYER_QUESTIONS
    )
    assert {
        answer: sum(item.answer == answer for item in PRO_PLAYER_QUESTIONS)
        for answer in "ABCD"
    } == {"A": 7, "B": 7, "C": 7, "D": 7}
    cycle = [
        question_for("group", date(2026, 8, 20) + timedelta(days=offset)).key
        for offset in range(len(PRO_PLAYER_QUESTIONS))
    ]
    assert len(set(cycle)) == len(PRO_PLAYER_QUESTIONS)
    assert question_for("group", "2026-08-20").key == cycle[0]
    assert question_for(
        "group", date(2026, 8, 20) + timedelta(days=len(PRO_PLAYER_QUESTIONS))
    ).key == cycle[0]
    assert question_by_key(LEGACY_GRENADE_QUESTIONS[0].key) == LEGACY_GRENADE_QUESTIONS[0]
    assert (
        question_by_key(LEGACY_PRO_PLAYER_QUESTIONS_V1[0].key)
        == LEGACY_PRO_PLAYER_QUESTIONS_V1[0]
    )

    question_text = format_quiz_question(question)
    reveal_text = format_quiz_reveal(
        QuizReveal("group", "2026-07-24", question, (), "", ())
    )
    assert "CS 职业选手每日一题" in question_text
    assert "#选手 <A/B/C/D>" in question_text
    assert "CS 职业选手每日一题揭晓" in reveal_text


def test_museum_respects_opt_out_and_subject_can_delete(tmp_path: Path) -> None:
    repository, service = _services(tmp_path)
    source = HistorySource("message-1", "10002", "精灵", "我绝对不会白给")

    added = service.add_history("group", "10001", "经典发言", source)
    assert "黑历史 #1" in added
    assert "我绝对不会白给" in service.history("group", "1")
    assert "已删除" in service.delete_history("group", "10002", "member", "1")

    service.set_history("group", "10002", enabled=False)
    with pytest.raises(CommunityError, match="关闭"):
        service.add_history(
            "group",
            "10001",
            "",
            HistorySource("message-2", "10002", "精灵", "再来一把"),
        )


def test_revenge_tracking_is_idempotent_and_detects_comeback(tmp_path: Path) -> None:
    repository, _service = _services(tmp_path)

    first = repository.process_revenge(
        "group", "match-1", [("10001", "牛牛", "10002", "精灵")]
    )
    duplicate = repository.process_revenge(
        "group", "match-1", [("10001", "牛牛", "10002", "精灵")]
    )
    second = repository.process_revenge(
        "group", "match-2", [("10002", "精灵", "10001", "牛牛")]
    )

    assert first[0].kind == "new"
    assert duplicate == ()
    assert second[0].kind == "revenge"
    assert second[0].previous_streak == 1
