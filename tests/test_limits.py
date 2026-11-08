import json
from pathlib import Path
from typing import Any

import httpx

from pwcs2_bot.channel import PWCS2Config
from pwcs2_bot.commands import CommandService, ParsedCommand
from pwcs2_bot.database import BindingRepository
from pwcs2_bot.perfectworld import PerfectWorldClient


class FakeApi:
    def __init__(self) -> None:
        self.queries: list[str] = []

    async def detail_stats(self, steam_id: str) -> dict[str, Any]:
        self.queries.append(steam_id)
        return {"name": "Player", "steamId": steam_id}


async def test_per_member_query_cooldown(tmp_path: Path) -> None:
    now = [100.0]
    api = FakeApi()
    service = CommandService(
        BindingRepository(tmp_path / "bindings.db"),
        api,
        query_cooldown_seconds=5,
        clock=lambda: now[0],
    )
    await service.handle(
        ParsedCommand("bind", "76561198929215155"),
        group_id="group",
        qq_id="123456",
    )

    first = await service.handle(ParsedCommand("stats"), group_id="group", qq_id="123456")
    throttled = await service.handle(ParsedCommand("stats"), group_id="group", qq_id="123456")
    now[0] += 5
    after_cooldown = await service.handle(
        ParsedCommand("stats"), group_id="group", qq_id="123456"
    )

    assert "CS2 完美数据" in first
    assert "查询过于频繁，请 5 秒后重试" in throttled
    assert "CS2 完美数据" in after_cooldown
    assert api.queries == ["76561198929215155", "76561198929215155"]


async def test_api_cache_has_lru_size_limit() -> None:
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        steam_id = str(json.loads(request.content)["toSteamId"])
        requests.append(steam_id)
        return httpx.Response(
            200,
            json={"statusCode": 0, "data": {"name": "Player", "steamId": steam_id}},
        )

    client = PerfectWorldClient(
        "token",
        base_url="https://example.test",
        transport=httpx.MockTransport(handler),
        cache_ttl_seconds=60,
        max_cache_entries=2,
    )
    a = "76561198929215155"
    b = "76561198929215156"
    c = "76561198929215157"
    try:
        await client.detail_stats(a)
        await client.detail_stats(b)
        await client.detail_stats(a)
        await client.detail_stats(c)
        await client.detail_stats(b)
    finally:
        await client.close()

    assert requests == [a, b, c, b]
    assert len(client._cache) == 2


def test_channel_has_safe_default_limits() -> None:
    config = PWCS2Config()
    assert config.query_cooldown_seconds == 5
    assert config.cache_ttl_seconds == 60
    assert config.stats_image_enabled is True
    assert config.stats_image_timeout_seconds == 45
    assert config.match_notifications_enabled is True
    assert config.match_poll_interval_seconds == 180
    assert config.match_notifications_per_poll == 3
    assert config.steam_presence_proxy_url == ""
    assert config.steam_presence_cache_ttl_seconds == 45
    assert config.steam_presence_stale_ttl_seconds == 900
    assert config.steam_presence_failure_cooldown_seconds == 15
    assert config.membership_sync_enabled is True
    assert config.membership_sync_interval_seconds == 21600
    assert config.group_card_lock_enabled is True
    assert config.group_card_lock_reconcile_interval_seconds == 300
    assert config.group_card_lock_reconcile_initial_delay_seconds == 15
    assert config.unbound_guard_enabled is False
    assert config.unbound_daily_message_limit == 10
    assert config.unbound_mute_duration_seconds == 600
    assert config.daily_summary_enabled is True
    assert config.daily_summary_time == "23:30"
    assert config.daily_summary_timezone == "Asia/Shanghai"
    assert config.daily_summary_match_page_size == 50
    assert config.skin_price_enabled is True
    assert config.skin_price_time == "12:00"
    assert config.skin_price_max_items_per_group == 20
    assert config.skin_price_request_timeout_seconds == 20
    assert config.skin_price_localization_cache_ttl_seconds == 86400
    assert config.group_level_removal_grace_days == 15
    assert config.group_level_removal_max_per_day == 10
    assert config.ai_model == "deepseek-v4-flash-ascend"
    assert config.ai_vision_model == "qwen3.6-chat"
    assert config.ai_vision_max_tokens == 1024
    assert config.ai_cooldown_seconds == 8
    assert config.ai_max_concurrent_requests == 20
    assert config.ai_group_shared_context_enabled is True
    assert config.ai_allow_concurrent_same_chat is True
    assert config.forward_unmatched is False
