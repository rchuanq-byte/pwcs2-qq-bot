from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest

from pwcs2_bot.database import BindingRepository
from pwcs2_bot.skin_prices import (
    DailySkinPriceService,
    SkinPriceClient,
    SkinPriceError,
    SkinPriceReport,
    SkinQuote,
    parse_market_name,
)

TZ = ZoneInfo("Asia/Shanghai")
REDLINE = "AK-47 | Redline (Field-Tested)"
SLATE = "AK-47 | Slate (Field-Tested)"


def test_parse_market_name_accepts_name_and_steam_listing() -> None:
    assert parse_market_name(REDLINE) == REDLINE
    assert (
        parse_market_name(
            "https://steamcommunity.com/market/listings/730/"
            "AK-47%20%7C%20Redline%20%28Field-Tested%29"
        )
        == REDLINE
    )
    with pytest.raises(SkinPriceError, match="只支持 Steam"):
        parse_market_name("https://example.com/market/listings/730/item")


async def test_client_resolves_names_converts_cny_and_caches() -> None:
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        if request.url.host == "prices.example":
            return httpx.Response(
                200,
                json={
                    REDLINE: {
                        "starting_at": {"price": 10.0},
                        "highest_order": {"price": 9.5},
                    },
                    SLATE: {
                        "starting_at": {"price": 2.0},
                        "highest_order": {"price": 1.8},
                    },
                },
            )
        return httpx.Response(200, json={"rates": {"CNY": 7.2}})

    client = SkinPriceClient(
        price_url="https://prices.example/buff.json",
        exchange_url="https://rates.example/usd",
        transport=httpx.MockTransport(handler),
    )
    try:
        assert await client.resolve_name("redline") == REDLINE
        assert await client.resolve_name(
            "https://steamcommunity.com/market/listings/730/"
            "AK-47%20%7C%20Redline%20%28Field-Tested%29"
        ) == REDLINE
        quote = (await client.quotes((REDLINE,)))[0]
        assert quote.sell_cny == pytest.approx(72.0)
        assert quote.buy_cny == pytest.approx(68.4)
        await client.quotes((SLATE,))
    finally:
        await client.close()

    assert len(requests) == 2


async def test_client_reports_ambiguous_partial_name() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={REDLINE: {}, SLATE: {}})

    client = SkinPriceClient(
        price_url="https://prices.example/buff.json",
        exchange_url="https://rates.example/usd",
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(SkinPriceError, match="匹配到多个"):
            await client.resolve_name("AK-47")
    finally:
        await client.close()


async def test_quote_keeps_price_when_preview_catalog_is_unavailable(
    monkeypatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "prices.example":
            return httpx.Response(
                200,
                json={REDLINE: {"starting_at": {"price": 10.0}}},
            )
        return httpx.Response(200, json={"rates": {"CNY": 7.2}})

    client = SkinPriceClient(
        price_url="https://prices.example/buff.json",
        exchange_url="https://rates.example/usd",
        transport=httpx.MockTransport(handler),
    )

    async def unavailable(_catalog) -> dict:
        raise SkinPriceError("preview unavailable")

    monkeypatch.setattr(client, "_get_localized_aliases", unavailable)
    try:
        quote = (await client.quotes((REDLINE,), include_images=True))[0]
    finally:
        await client.close()

    assert quote.sell_cny == pytest.approx(72.0)
    assert quote.image_url == ""


async def test_client_supports_chinese_fuzzy_skin_search() -> None:
    normal_ft = REDLINE
    normal_mw = "AK-47 | Redline (Minimal Wear)"
    stattrak_ft = "StatTrak™ AK-47 | Redline (Field-Tested)"
    price_catalog = {
        normal_ft: {},
        normal_mw: {},
        stattrak_ft: {},
    }
    english = [
        {
            "id": "skin-redline",
            "name": "AK-47 | Redline",
            "image": "https://community.akamai.steamstatic.com/economy/image/redline",
            "weapon": {"id": "weapon_ak47", "name": "AK-47"},
            "pattern": {"id": "cu_redline", "name": "Redline"},
            "stattrak": True,
            "souvenir": False,
            "wears": [
                {"id": "SFUI_InvTooltip_Wear_Amount_1", "name": "Minimal Wear"},
                {"id": "SFUI_InvTooltip_Wear_Amount_2", "name": "Field-Tested"},
            ],
        }
    ]
    chinese = [
        {
            "id": "skin-redline",
            "name": "AK-47 | 红线",
            "weapon": {"id": "weapon_ak47", "name": "AK-47"},
            "pattern": {"id": "cu_redline", "name": "红线"},
            "stattrak": True,
            "souvenir": False,
            "wears": [
                {"id": "SFUI_InvTooltip_Wear_Amount_1", "name": "略有磨损"},
                {"id": "SFUI_InvTooltip_Wear_Amount_2", "name": "久经沙场"},
            ],
        }
    ]
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.host or "")
        if request.url.host == "prices.example":
            return httpx.Response(200, json=price_catalog)
        if request.url.host == "en.example":
            return httpx.Response(200, json=english)
        if request.url.host == "zh.example":
            return httpx.Response(200, json=chinese)
        return httpx.Response(200, json={"rates": {"CNY": 7.2}})

    client = SkinPriceClient(
        price_url="https://prices.example/buff.json",
        exchange_url="https://rates.example/usd",
        skins_en_url="https://en.example/skins.json",
        skins_zh_url="https://zh.example/skins.json",
        transport=httpx.MockTransport(handler),
    )
    try:
        assert await client.resolve_name("ak红线久经") == normal_ft
        assert await client.resolve_name("暗金ak红线久经") == stattrak_ft
        assert await client.resolve_name("红线略磨") == normal_mw
        quote = (await client.quotes((normal_ft,), include_images=True))[0]
        assert quote.image_url.endswith("/redline")
        with pytest.raises(SkinPriceError, match="匹配到多个") as exc:
            await client.resolve_name("红线")
        assert "AK-47 | 红线" in str(exc.value)
    finally:
        await client.close()

    assert requests.count("prices.example") == 1
    assert requests.count("en.example") == 1
    assert requests.count("zh.example") == 1


class FakeClient:
    def __init__(self, sell_usd: float = 11.0) -> None:
        self.sell_usd = sell_usd
        self.closed = False

    async def resolve_name(self, value: str) -> str:
        if "redline" in value.casefold() or "Redline" in value:
            return REDLINE
        raise SkinPriceError("没有找到该饰品")

    async def quotes(
        self,
        names,
        *,
        include_images: bool = False,
    ) -> tuple[SkinQuote, ...]:
        return tuple(
            SkinQuote(
                name,
                self.sell_usd,
                self.sell_usd - 1,
                7.0,
                "https://community.akamai.steamstatic.com/economy/image/test"
                if include_images
                else "",
            )
            for name in names
        )

    async def close(self) -> None:
        self.closed = True


async def test_members_manage_group_watchlist_with_owner_protection(tmp_path: Path) -> None:
    repository = BindingRepository(tmp_path / "db.sqlite")
    service = DailySkinPriceService(
        repository,
        FakeClient(),
        lambda _group, _content: None,
        max_items_per_group=2,
    )

    added = await service.add_watch("group", "qq-1", "redline")
    duplicate = await service.add_watch("group", "qq-2", REDLINE)

    assert "¥77.00" in added
    assert "已在关注列表" in duplicate
    assert REDLINE in service.watchlist("group")
    with pytest.raises(SkinPriceError, match="只能删除自己"):
        service.remove_watch("group", "qq-2", "1")
    assert "已删除" in service.remove_watch("group", "qq-1", "1")
    assert "关注列表为空" in service.watchlist("group")


async def test_daily_report_uses_previous_snapshot_and_sends_once(tmp_path: Path) -> None:
    repository = BindingRepository(tmp_path / "db.sqlite")
    repository.add_skin_watch("group", REDLINE, "qq-1", max_items=20)
    repository.save_skin_price_snapshot(
        group_id="group",
        market_hash_name=REDLINE,
        snapshot_date="2026-07-02",
        sell_price_cny=70,
        buy_price_cny=63,
        source_price_usd=10,
        usd_cny_rate=7,
    )
    sent: list[tuple[str, SkinPriceReport]] = []

    async def capture(group_id: str, report: SkinPriceReport) -> None:
        sent.append((group_id, report))

    service = DailySkinPriceService(
        repository,
        FakeClient(sell_usd=11),
        capture,
        send_time="12:00",
        initial_delay_seconds=0,
        allowed_groups=["group"],
    )

    assert await service.run_due_once(datetime(2026, 7, 3, 11, 59, tzinfo=TZ)) == 0
    assert await service.run_due_once(datetime(2026, 7, 3, 12, 0, tzinfo=TZ)) == 1
    assert await service.run_due_once(datetime(2026, 7, 3, 13, 0, tzinfo=TZ)) == 0

    assert len(sent) == 1
    report = sent[0][1]
    assert "2026-07-03" in report.text
    assert "↑ +10.00%" in report.text
    assert report.entries[0].quote.image_url.endswith("/test")
    assert repository.latest_skin_sell_price("group", REDLINE) == pytest.approx(77)
