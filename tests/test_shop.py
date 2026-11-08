from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path

import pytest

from pwcs2_bot.appearance import STATS_BACKGROUNDS, resolve_stats_background
from pwcs2_bot.commands import CommandService, ParsedCommand
from pwcs2_bot.community import BADGES, CommunityError, CommunityRepository, GroupPlayService
from pwcs2_bot.database import BindingRepository
from pwcs2_bot.formatting import build_stats_snapshot
from pwcs2_bot.match_formatting import build_match_history
from pwcs2_bot.shop import SHOP_ITEMS, daily_offers, resolve_shop_item
from pwcs2_bot.stats_card import render_match_history_html, render_stats_html

G, A, B = "test-group", "10001", "10002"
DAY = "2026-09-30"


@pytest.fixture
def shop(tmp_path: Path) -> GroupPlayService:
    path = tmp_path / "shop.db"
    bindings = BindingRepository(path)
    bindings.upsert_group_member_identity(G, A, "牛牛")
    bindings.upsert_group_member_identity(G, B, "精灵")
    repository = CommunityRepository(path)
    repository.add_coins(G, A, 5000, reason="test", reference_key="seed")
    return GroupPlayService(repository, bindings, prediction_enabled=False)


@pytest.mark.parametrize(("query", "key"), [
    ("6", "6"), ("答题提示卡", "6"), ("提示", "6"), ("赛博霓虹", "8"),
    ("战绩背景·冰川蓝图", "12"), ("补签", "10"), ("极光", "11"),
])
def test_resolve_shop_item_by_number_or_unambiguous_name(query, key):
    assert resolve_shop_item(query) == key


@pytest.mark.parametrize("query", ["", "0", "99", "免费牛币", "卡", "背景", "称号"])
def test_unknown_or_ambiguous_names_never_choose_something_to_charge(query):
    with pytest.raises(ValueError):
        resolve_shop_item(query)


def test_catalog_is_consistent_with_background_prices():
    for theme in STATS_BACKGROUNDS.values():
        if theme.shop_item:
            assert SHOP_ITEMS[theme.shop_item][1] == theme.price
            assert theme.name in SHOP_ITEMS[theme.shop_item][0]
            assert resolve_stats_background(theme.name) == theme.key


def test_bulk_purchase_by_name_is_atomic_and_persistent(shop):
    repository = shop.repository
    before = repository.wallet(G, A)[0]
    assert "已放入背包 ×3" in shop.buy(G, A, "答题提示卡 3", request_id="purchase-1")
    assert repository.wallet(G, A)[0] == before - 180
    assert repository.inventory(G, A)["quiz_hint"] == 3
    reloaded = CommunityRepository(repository.path)
    result = reloaded.purchase(G, A, "6", quantity=3, request_id="purchase-1")
    assert result[1] == 180
    assert reloaded.wallet(G, A)[0] == before - 180
    assert reloaded.inventory(G, A)["quiz_hint"] == 3
    with pytest.raises(CommunityError, match="同一条消息"):
        reloaded.purchase(G, A, "3", request_id="purchase-1")


@pytest.mark.parametrize("quantity", [0, -1, 21, 1.5, True, "2"])
def test_repository_rejects_invalid_quantities_without_debit(shop, quantity):
    before = shop.repository.wallet(G, A)
    with pytest.raises(CommunityError, match="数量"):
        shop.repository.purchase(G, A, "2", quantity=quantity)
    assert shop.repository.wallet(G, A) == before
    assert shop.repository.inventory(G, A) == {}


def test_insufficient_balance_rolls_back_wallet_ledger_and_inventory(shop):
    repository = shop.repository
    with pytest.raises(CommunityError, match="余额不足"):
        repository.purchase(G, B, "2")
    assert repository.peek_wallet(G, B) is None
    assert repository.inventory(G, B) == {}
    with repository._connect() as conn:
        ledger_count = conn.execute(
            "SELECT COUNT(*) FROM coin_ledger WHERE qq_id = ?", (B,)
        ).fetchone()[0]
        receipt_count = conn.execute(
            "SELECT COUNT(*) FROM shop_receipts WHERE qq_id = ?", (B,)
        ).fetchone()[0]
        assert ledger_count == receipt_count == 0


def test_daily_offers_are_stable_and_price_is_server_computed(shop):
    offers = daily_offers(G, DAY)
    assert len(offers) == 3
    assert offers == daily_offers(G, DAY)
    assert "7" not in offers
    key, price = next(iter(offers.items()))
    assert price == (SHOP_ITEMS[key][1] * 8 + 9) // 10
    before = shop.repository.wallet(G, A)[0]
    shop.buy(G, A, f"特惠 {key}", shop_date=DAY, request_id="daily-1")
    assert shop.repository.wallet(G, A)[0] == before - price
    assert "已购买" in shop.specials(G, A, shop_date=DAY)
    # Delivery retries after midnight must return the old receipt, not buy again.
    shop.buy(G, A, f"特惠 {key}", shop_date="2026-10-01", request_id="daily-1")
    assert shop.repository.wallet(G, A)[0] == before - price
    with pytest.raises(CommunityError, match="今天已经买过"):
        shop.buy(G, A, f"特惠 {key}", shop_date=DAY, request_id="daily-2")
    with pytest.raises(CommunityError, match="限购"):
        shop.buy(G, A, f"特惠 {key} 2", shop_date=DAY)
    with pytest.raises(CommunityError, match="不在今日特惠"):
        shop.buy(G, A, "特惠 8", shop_date=DAY)
    assert shop.repository.wallet(G, A)[0] == before - price


def test_daily_limit_is_atomic_under_concurrent_purchases(shop):
    key, price = next(iter(daily_offers(G, DAY).items()))
    before = shop.repository.wallet(G, A)[0]

    def purchase(index):
        try:
            shop.repository.purchase(
                G, A, key, discounted=True, shop_date=DAY, request_id=f"concurrent-{index}",
            )
            return True
        except CommunityError:
            return False

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(purchase, range(12)))
    assert sum(results) == 1
    assert shop.repository.wallet(G, A)[0] == before - price
    assert sum(shop.repository.inventory(G, A).values()) == 1


def test_concurrent_retries_of_same_message_charge_only_once(shop):
    repo = shop.repository
    before = repo.wallet(G, A)[0]

    def purchase(_index):
        return repo.purchase(G, A, "6", quantity=3, request_id="same-message")

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(purchase, range(12)))
    assert all(result == results[0] for result in results)
    assert repo.wallet(G, A)[0] == before - 180
    assert repo.inventory(G, A)["quiz_hint"] == 3


def test_daily_quota_is_per_member_group_and_date(shop):
    repo = shop.repository
    key = next(iter(daily_offers(G, DAY)))
    repo.add_coins(G, B, 1000, reason="test", reference_key="fund-b")
    for qq in (A, B):
        repo.purchase(G, qq, key, discounted=True, shop_date=DAY)
        assert repo.daily_purchases(G, qq, DAY) == {key}
    assert repo.daily_purchases("other-group", A, DAY) == set()
    next_day = date.fromisoformat(DAY) + timedelta(days=1)
    while key not in daily_offers(G, next_day.isoformat()):
        next_day += timedelta(days=1)
    repo.purchase(G, A, key, discounted=True, shop_date=next_day.isoformat())
    assert sum(repo.inventory(G, A).values()) == 2


@pytest.mark.parametrize("operation", ["buy", "gift"])
def test_fulfillment_failure_rolls_back_all_debits_and_items(shop, monkeypatch, operation):
    repo = shop.repository
    shop.buy(G, A, "提示卡 3")
    balance = repo.wallet(G, A)
    inventory = repo.inventory(G, A)
    original = repo._add_inventory

    def fail_after_grant(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("simulated fulfillment failure")

    monkeypatch.setattr(repo, "_add_inventory", fail_after_grant)
    with pytest.raises(RuntimeError, match="fulfillment failure"):
        if operation == "buy":
            repo.purchase(G, A, "6", quantity=2, request_id="failed-fulfillment")
        else:
            repo.gift_item(G, A, B, "6", quantity=2, request_id="failed-fulfillment")
    assert repo.wallet(G, A) == balance
    assert repo.inventory(G, A) == inventory
    assert repo.inventory(G, B) == {}
    with repo._connect() as conn:
        assert conn.execute(
            "SELECT 1 FROM shop_receipts WHERE request_key = 'message:failed-fulfillment'"
        ).fetchone() is None
    monkeypatch.setattr(repo, "_add_inventory", original)
    if operation == "buy":
        repo.purchase(G, A, "6", quantity=2, request_id="failed-fulfillment")
        assert repo.wallet(G, A)[0] == balance[0] - 120
    else:
        repo.gift_item(G, A, B, "6", quantity=2, request_id="failed-fulfillment")
        assert repo.inventory(G, B)["quiz_hint"] == 2


def test_insufficient_balance_does_not_use_daily_limit(shop):
    key, price = next(iter(daily_offers(G, DAY).items()))
    repo = CommunityRepository(shop.repository.path, initial_balance=0)
    with pytest.raises(CommunityError, match="余额不足"):
        repo.purchase(G, B, key, discounted=True, shop_date=DAY, request_id="no-money")
    assert repo.daily_purchases(G, B, DAY) == set()
    assert repo.peek_wallet(G, B) is None
    repo.add_coins(G, B, price, reason="test", reference_key="retry-funding")
    repo.purchase(G, B, key, discounted=True, shop_date=DAY, request_id="no-money")
    assert repo.wallet(G, B)[0] == 0


def test_gift_moves_inventory_without_minting_coins_or_duplicate_delivery(shop):
    shop.buy(G, A, "提示卡 3")
    balance = shop.repository.wallet(G, A)
    assert "未扣牛币" in shop.gift(G, A, f"@{B} 答题提示卡 2", request_id="gift-1")
    shop.gift(G, A, f"@{B} 答题提示卡 2", request_id="gift-1")
    assert shop.repository.inventory(G, A)["quiz_hint"] == 1
    assert shop.repository.inventory(G, B)["quiz_hint"] == 2
    assert shop.repository.wallet(G, A) == balance
    assert shop.repository.peek_wallet(G, B) is None
    assert shop.repository.inventory("other-group", B) == {}


@pytest.mark.parametrize("argument", [
    f"@{A} 提示卡", "@123456 提示卡", f"@{B} 1", f"@{B} 8", f"@{B} 提示卡 2",
])
def test_invalid_gifts_leave_sender_inventory_untouched(shop, argument):
    shop.buy(G, A, "提示卡")
    before = shop.repository.wallet(G, A)
    with pytest.raises(CommunityError):
        shop.gift(G, A, argument)
    assert shop.repository.inventory(G, A)["quiz_hint"] == 1
    assert shop.repository.inventory(G, B) == {}
    assert shop.repository.wallet(G, A) == before


def test_concurrent_gifts_cannot_spend_same_item_twice(shop):
    shop.buy(G, A, "提示卡")

    def give(index):
        try:
            shop.repository.gift_item(G, A, B, "6", request_id=f"gift-race-{index}")
            return True
        except CommunityError:
            return False

    with ThreadPoolExecutor(max_workers=4) as pool:
        assert sum(pool.map(give, range(8))) == 1
    assert shop.repository.inventory(G, A) == {}
    assert shop.repository.inventory(G, B)["quiz_hint"] == 1


def test_collection_keeps_custom_titles_and_can_equip_for_free(shop):
    shop.buy(G, A, "自选称号券 2")
    shop.use_custom_badge(G, A, "残局指挥官")
    shop.use_custom_badge(G, A, "道具工程师")
    assert shop.repository.owned_badges(G, A) == ["残局指挥官", "道具工程师"]
    balance = shop.repository.wallet(G, A)[0]
    assert "免费佩戴" in shop.equip_badge(G, A, "1")
    assert shop.equipped_badge(G, A) == "残局指挥官"
    assert "卸下" in shop.equip_badge(G, A, "0")
    # Legacy #使用称号 also re-equips owned titles without consuming a voucher.
    shop.use_custom_badge(G, A, "道具工程师")
    assert shop.repository.wallet(G, A)[0] == balance
    assert shop.repository.inventory(G, A) == {}
    assert shop.equipped_badge(G, A) == "道具工程师"
    assert "2 个" in shop.badge_collection(G, A)
    assert shop.repository.owned_badges("other-group", A) == []
    with pytest.raises(CommunityError):
        shop.equip_badge(G, B, "残局指挥官")


def test_gifted_custom_badge_voucher_works_for_member_without_wallet(shop):
    shop.buy(G, A, "自选称号券")
    shop.gift(G, A, f"@{B} 自选称号券")
    assert shop.repository.peek_wallet(G, B) is None
    shop.use_custom_badge(G, B, "道具工程师")
    assert shop.equipped_badge(G, B) == "道具工程师"
    assert shop.repository.owned_badges(G, B) == ["道具工程师"]
    assert shop.repository.inventory(G, B) == {}


def test_blind_boxes_never_repeat_and_stop_charging_after_completion(shop):
    for _ in BADGES:
        shop.buy(G, A, "称号盲盒")
    assert set(shop.repository.owned_badges(G, A)) == set(BADGES)
    before = shop.repository.wallet(G, A)
    with pytest.raises(CommunityError, match="全部集齐"):
        shop.buy(G, A, "称号盲盒")
    assert shop.repository.wallet(G, A) == before
    assert len(shop.repository.owned_badges(G, A)) == len(BADGES)


def test_upgrade_preserves_legacy_badge_balance_inventory_and_selection(shop):
    shop.buy(G, A, "提示卡 2")
    shop.buy(G, A, "霓虹")
    with shop.repository._connect() as conn:
        conn.execute("DROP TABLE member_badges")
        conn.execute("UPDATE coin_wallets SET badge = '旧版专属称号' WHERE qq_id = ?", (A,))
    before = shop.repository.wallet(G, A)
    upgraded = CommunityRepository(shop.repository.path)
    assert upgraded.wallet(G, A) == before
    assert upgraded.owned_badges(G, A) == ["旧版专属称号"]
    assert upgraded.inventory(G, A)["quiz_hint"] == 2
    assert upgraded.stats_background(G, A) == "cyber_neon"
    again = CommunityRepository(shop.repository.path)
    assert again.owned_badges(G, A) == ["旧版专属称号"]


def test_repair_restores_streak_once_without_retroactive_coins(shop):
    repo = shop.repository
    for offset in range(5):
        repo.checkin(G, A, (date(2026, 9, 24) + timedelta(days=offset)).isoformat())
    shop.buy(G, A, "补签卡 2")
    before = repo.wallet(G, A)[0]
    assert "连续 6 天" in shop.repair_checkin(G, A, checkin_date=DAY, request_id="repair-1")
    shop.repair_checkin(G, A, checkin_date=DAY, request_id="repair-1")
    assert repo.wallet(G, A)[0] == before
    assert repo.inventory(G, A)["checkin_repair"] == 1
    with repo._connect() as conn:
        row = conn.execute(
            "SELECT * FROM daily_checkins WHERE checkin_date = '2026-09-29'"
        ).fetchone()
        assert row["base_reward"] == row["streak_bonus"] == 0
    today = repo.checkin(G, A, DAY)
    assert today.streak == 7 and today.streak_bonus == 15
    assert repo.wallet(G, A)[0] == before + 20
    with pytest.raises(CommunityError, match="未消耗道具"):
        shop.repair_checkin(G, A, checkin_date=DAY, request_id="repair-2")
    assert repo.inventory(G, A)["checkin_repair"] == 1


def test_repair_rejects_no_prior_streak_without_consuming(shop):
    shop.buy(G, A, "补签卡")
    with pytest.raises(CommunityError, match="前天没有"):
        shop.repair_checkin(G, A, checkin_date=DAY)
    assert shop.repository.inventory(G, A)["checkin_repair"] == 1
    assert shop.repository.checkin(G, A, DAY).streak == 1


@pytest.mark.parametrize("key", ["11", "12", "13"])
def test_new_backgrounds_apply_to_cards_without_replacing_game_id(shop, key):
    shop.buy(G, A, key)
    theme = STATS_BACKGROUNDS[shop.stats_background_key(G, A)]
    assert theme.css_background and theme.asset_filename is None
    stats = build_stats_snapshot({"name": "Actual Game ID", "steamId": "76561197960266101"})
    history = build_match_history([], steam_id=stats.steam_id, profile={"name": stats.name})
    for html in [
        render_stats_html(stats, background_key=theme.key, member_badge="我的称号"),
        render_match_history_html(history, background_key=theme.key, member_badge="我的称号"),
    ]:
        assert theme.css_background in html
        assert stats.name in html and "我的称号" in html
    before = shop.repository.wallet(G, A)[0]
    with pytest.raises(CommunityError, match="永久拥有"):
        shop.buy(G, A, key)
    assert shop.repository.wallet(G, A)[0] == before


def test_categories_and_disabled_prediction_cannot_be_bypassed_by_name(shop):
    assert "永久背景" not in shop.shop(G, A, "道具")
    assert "极光终夜" in shop.shop(G, A, "背景")
    assert "双倍答题卡" not in shop.shop(G, A, "背景")
    assert "竞猜保险" not in shop.shop(G, A)
    for query in ("7", "竞猜保险", "保险 2"):
        with pytest.raises(CommunityError, match="竞猜功能已关闭"):
            shop.buy(G, A, query)


async def test_shop_commands_pass_message_ids_and_calendar_to_transactions(shop):
    commands = CommandService(shop.bindings, object(), group_play=shop)
    for text, action in [
        ("/牛牛商店", "shop"), ("#特惠", "shop_specials"),
        ("#称号", "badge_collection"), ("#佩戴称号 0", "badge_equip"),
        ("#赠送 @10002 提示卡", "shop_gift"), ("#补签", "checkin_repair"),
    ]:
        assert commands.parse(text).action == action
    for _ in range(2):
        result = await commands.handle_result(
            ParsedCommand("buy", "提示卡 2"), group_id=G, qq_id=A,
            local_date=DAY, request_id="one-message",
        )
        assert "购买成功" in result.content
    assert shop.repository.inventory(G, A)["quiz_hint"] == 2
    result = await commands.handle_result(
        ParsedCommand("shop", "特惠"), group_id=G, qq_id=A, local_date=DAY,
    )
    assert DAY in result.content
