import asyncio

import httpx
import pytest

import pwcs2_bot.steam_presence as steam_presence_module
from pwcs2_bot.steam_presence import (
    SteamPresence,
    SteamPresenceClient,
    SteamPresenceError,
    build_group_game_status_snapshot,
    build_player_game_status_snapshot,
    format_group_game_status,
    format_player_game_status,
)


def _install_route_clients(
    monkeypatch: pytest.MonkeyPatch,
    proxy_handler: httpx.AsyncBaseTransport,
    direct_handler: httpx.AsyncBaseTransport,
) -> list[dict[str, object]]:
    real_async_client = httpx.AsyncClient
    clients = [
        real_async_client(
            base_url="https://api.steampowered.com",
            transport=proxy_handler,
        ),
        real_async_client(
            base_url="https://api.steampowered.com",
            transport=direct_handler,
        ),
    ]
    options: list[dict[str, object]] = []

    def client_factory(**kwargs: object) -> httpx.AsyncClient:
        options.append(kwargs)
        return clients[len(options) - 1]

    monkeypatch.setattr(steam_presence_module.httpx, "AsyncClient", client_factory)
    return options


@pytest.mark.asyncio
async def test_summaries_parses_cs2_server_presence_and_batches() -> None:
    seen: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "response": {
                    "players": [
                        {
                            "steamid": "76561198000000001",
                            "personaname": "牛牛",
                            "gameid": "730",
                            "gameextrainfo": "Counter-Strike 2",
                            "personastate": 1,
                            "gameserverip": "1.2.3.4:27015",
                        }
                    ]
                }
            },
        )

    client = SteamPresenceClient(
        "a" * 32,
        transport=httpx.MockTransport(handler),
    )
    try:
        result = await client.summaries(["76561198000000001"])
    finally:
        await client.close()

    assert result["76561198000000001"].is_cs2_server
    assert result["76561198000000001"].is_playing
    assert result["76561198000000001"].persona_name == "牛牛"
    assert result["76561198000000001"].display_game == "Counter-Strike 2"
    assert result["76561198000000001"].persona_state == 1
    assert seen[0].headers["x-webapi-key"] == "a" * 32
    assert "key" not in seen[0].url.params


@pytest.mark.asyncio
async def test_missing_key_is_not_configured() -> None:
    client = SteamPresenceClient("${STEAM_WEB_API_KEY}")
    try:
        assert client.configured is False
        with pytest.raises(SteamPresenceError, match="STEAM_WEB_API_KEY"):
            await client.summaries(["76561198000000001"])
    finally:
        await client.close()


def test_presence_base_url_must_use_https() -> None:
    with pytest.raises(ValueError, match="HTTPS"):
        SteamPresenceClient("a" * 32, base_url="http://api.steampowered.com")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "proxy_url",
    [
        "http://127.0.0.1:7890",
        "https://proxy.example:8443",
        "http://user:p%40ss@proxy.example:7890/",
    ],
)
async def test_presence_proxy_accepts_safe_http_urls(proxy_url: str) -> None:
    client = SteamPresenceClient("a" * 32, proxy_url=proxy_url)
    await client.close()


@pytest.mark.parametrize(
    "proxy_url",
    [
        "socks5://127.0.0.1:7891",
        "file:///tmp/proxy.sock",
        "http:///missing-host",
        "http://proxy.example:7890/path",
        "http://proxy.example:7890/?token=secret",
        "http://proxy.example:7890/#fragment",
        "http://proxy.example:99999",
        "http://proxy.example:7890/\nInjected",
    ],
)
def test_presence_proxy_rejects_unsafe_urls_without_echoing_them(proxy_url: str) -> None:
    with pytest.raises(ValueError, match="proxy URL") as raised:
        SteamPresenceClient("a" * 32, proxy_url=proxy_url)

    assert proxy_url not in str(raised.value)
    assert "secret" not in str(raised.value)


@pytest.mark.asyncio
async def test_presence_proxy_client_is_isolated_from_environment_and_both_clients_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instances: list[object] = []

    class RecordingClient:
        def __init__(self, **kwargs: object) -> None:
            self.options = kwargs
            self.closed = False
            instances.append(self)

        async def aclose(self) -> None:
            self.closed = True

    monkeypatch.setattr(steam_presence_module.httpx, "AsyncClient", RecordingClient)
    transport = httpx.MockTransport(lambda _request: httpx.Response(200))
    client = SteamPresenceClient(
        "a" * 32,
        proxy_url="http://user:password@proxy.example:7890",
        transport=transport,
    )

    await client.close()

    assert len(instances) == 2
    proxy_client, direct_client = instances
    assert proxy_client.options["proxy"] == "http://user:password@proxy.example:7890"
    assert proxy_client.options["trust_env"] is False
    assert "transport" not in proxy_client.options
    assert "proxy" not in direct_client.options
    assert direct_client.options["transport"] is transport
    assert direct_client.options["trust_env"] is False
    assert proxy_client.closed is True
    assert direct_client.closed is True


@pytest.mark.asyncio
async def test_placeholder_proxy_value_uses_only_direct_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instances: list[object] = []

    class RecordingClient:
        def __init__(self, **kwargs: object) -> None:
            self.options = kwargs
            instances.append(self)

        async def aclose(self) -> None:
            return None

    monkeypatch.setattr(steam_presence_module.httpx, "AsyncClient", RecordingClient)
    client = SteamPresenceClient(
        "a" * 32,
        proxy_url="${STEAM_PRESENCE_PROXY_URL}",
    )
    await client.close()

    assert len(instances) == 1
    assert "proxy" not in instances[0].options
    assert instances[0].options["trust_env"] is False


@pytest.mark.asyncio
async def test_configured_proxy_is_preferred_without_touching_direct_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proxy_calls = 0
    direct_calls = 0

    async def proxy_handler(_request: httpx.Request) -> httpx.Response:
        nonlocal proxy_calls
        proxy_calls += 1
        return httpx.Response(
            200,
            json={
                "response": {
                    "players": [
                        {
                            "steamid": "76561197960266101",
                            "personaname": "Proxy Player",
                            "personastate": 1,
                        }
                    ]
                }
            },
        )

    async def direct_handler(_request: httpx.Request) -> httpx.Response:
        nonlocal direct_calls
        direct_calls += 1
        raise AssertionError("direct route must not be used after proxy success")

    options = _install_route_clients(
        monkeypatch,
        httpx.MockTransport(proxy_handler),
        httpx.MockTransport(direct_handler),
    )
    client = SteamPresenceClient(
        "a" * 32,
        proxy_url="http://proxy.example:7890",
    )
    try:
        result = await client.summaries(["76561197960266101"])
    finally:
        await client.close()

    assert result["76561197960266101"].persona_name == "Proxy Player"
    assert proxy_calls == 1
    assert direct_calls == 0
    assert options[0]["proxy"] == "http://proxy.example:7890"
    assert "proxy" not in options[1]


@pytest.mark.asyncio
async def test_proxy_timeout_falls_back_to_direct_and_returns_fresh_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proxy_calls = 0
    direct_calls = 0

    async def proxy_handler(request: httpx.Request) -> httpx.Response:
        nonlocal proxy_calls
        proxy_calls += 1
        raise httpx.ReadTimeout("proxy unavailable", request=request)

    async def direct_handler(_request: httpx.Request) -> httpx.Response:
        nonlocal direct_calls
        direct_calls += 1
        return httpx.Response(
            200,
            json={
                "response": {
                    "players": [
                        {
                            "steamid": "76561197960266101",
                            "personaname": "Direct Fallback",
                            "personastate": 1,
                        }
                    ]
                }
            },
        )

    _install_route_clients(
        monkeypatch,
        httpx.MockTransport(proxy_handler),
        httpx.MockTransport(direct_handler),
    )
    client = SteamPresenceClient(
        "a" * 32,
        proxy_url="http://proxy.example:7890",
        retry_attempts=1,
    )
    try:
        result = await client.summaries(["76561197960266101"])
    finally:
        await client.close()

    assert result.stale is False
    assert result["76561197960266101"].persona_name == "Direct Fallback"
    assert proxy_calls == 1
    assert direct_calls == 1


@pytest.mark.asyncio
async def test_proxy_retryable_server_error_falls_back_to_direct(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    direct_calls = 0

    async def proxy_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, request=request)

    async def direct_handler(_request: httpx.Request) -> httpx.Response:
        nonlocal direct_calls
        direct_calls += 1
        return httpx.Response(200, json={"response": {"players": []}})

    _install_route_clients(
        monkeypatch,
        httpx.MockTransport(proxy_handler),
        httpx.MockTransport(direct_handler),
    )
    client = SteamPresenceClient(
        "a" * 32,
        proxy_url="http://proxy.example:7890",
    )
    try:
        result = await client.summaries(["76561197960266101"])
    finally:
        await client.close()

    assert result == {}
    assert result.stale is False
    assert direct_calls == 1


@pytest.mark.asyncio
async def test_proxy_auth_error_does_not_fall_back_to_direct(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    direct_calls = 0

    async def proxy_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, request=request)

    async def direct_handler(_request: httpx.Request) -> httpx.Response:
        nonlocal direct_calls
        direct_calls += 1
        return httpx.Response(200, json={"response": {"players": []}})

    _install_route_clients(
        monkeypatch,
        httpx.MockTransport(proxy_handler),
        httpx.MockTransport(direct_handler),
    )
    client = SteamPresenceClient(
        "a" * 32,
        proxy_url="http://proxy.example:7890",
    )
    try:
        with pytest.raises(SteamPresenceError, match="HTTP 403"):
            await client.summaries(["76561197960266101"])
    finally:
        await client.close()

    assert direct_calls == 0


@pytest.mark.asyncio
async def test_proxy_and_direct_must_both_fail_before_stale_cache_is_used(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proxy_calls = 0
    direct_calls = 0
    now = [0.0]

    async def proxy_handler(request: httpx.Request) -> httpx.Response:
        nonlocal proxy_calls
        proxy_calls += 1
        if proxy_calls > 1:
            raise httpx.ReadTimeout("proxy unavailable", request=request)
        return httpx.Response(
            200,
            json={
                "response": {
                    "players": [
                        {
                            "steamid": "76561197960266101",
                            "personaname": "Cached Player",
                            "personastate": 1,
                        }
                    ]
                }
            },
        )

    async def direct_handler(request: httpx.Request) -> httpx.Response:
        nonlocal direct_calls
        direct_calls += 1
        raise httpx.ReadTimeout("direct unavailable", request=request)

    _install_route_clients(
        monkeypatch,
        httpx.MockTransport(proxy_handler),
        httpx.MockTransport(direct_handler),
    )
    client = SteamPresenceClient(
        "a" * 32,
        proxy_url="http://proxy.example:7890",
        retry_attempts=1,
        cache_ttl_seconds=45,
        stale_ttl_seconds=300,
        clock=lambda: now[0],
    )
    try:
        fresh = await client.summaries(["76561197960266101"])
        now[0] = 60
        stale = await client.summaries(["76561197960266101"])
        repeated_stale = await client.summaries(["76561197960266101"])
    finally:
        await client.close()

    assert fresh.stale is False
    assert stale.stale is True
    assert repeated_stale.stale is True
    assert proxy_calls == 2
    assert direct_calls == 1


@pytest.mark.asyncio
async def test_summaries_retries_one_transient_timeout() -> None:
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ReadTimeout("slow upstream", request=request)
        return httpx.Response(
            200,
            json={
                "response": {
                    "players": [
                        {
                            "steamid": "76561197960266101",
                            "personaname": "Retry Player",
                            "personastate": 1,
                            "gameid": "730",
                            "gameextrainfo": "Counter-Strike 2",
                        }
                    ]
                }
            },
        )

    client = SteamPresenceClient(
        "a" * 32,
        transport=httpx.MockTransport(handler),
    )
    try:
        players = await client.summaries(["76561197960266101"])
    finally:
        await client.close()

    assert calls == 2
    assert players["76561197960266101"].display_game == "Counter-Strike 2"


@pytest.mark.asyncio
async def test_summaries_does_not_retry_client_error() -> None:
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(403, request=request)

    client = SteamPresenceClient(
        "a" * 32,
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(SteamPresenceError, match="HTTP 403"):
            await client.summaries(["76561197960266101"])
    finally:
        await client.close()

    assert calls == 1


@pytest.mark.asyncio
async def test_summaries_retries_rate_limit_response() -> None:
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, request=request)
        return httpx.Response(
            200,
            json={
                "response": {
                    "players": [
                        {
                            "steamid": "76561197960266101",
                            "personaname": "Rate Limit Recovered",
                            "personastate": 1,
                        }
                    ]
                }
            },
        )

    client = SteamPresenceClient(
        "a" * 32,
        transport=httpx.MockTransport(handler),
    )
    try:
        recovered = await client.summaries(["76561197960266101"])
    finally:
        await client.close()

    assert calls == 2
    assert recovered["76561197960266101"].persona_name == "Rate Limit Recovered"


@pytest.mark.asyncio
async def test_client_error_does_not_open_network_failure_cooldown() -> None:
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(403, request=request)
        return httpx.Response(
            200,
            json={
                "response": {
                    "players": [
                        {
                            "steamid": "76561197960266101",
                            "personaname": "Recovered Player",
                            "personastate": 1,
                        }
                    ]
                }
            },
        )

    client = SteamPresenceClient(
        "a" * 32,
        retry_attempts=1,
        failure_cooldown_seconds=300,
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(SteamPresenceError, match="HTTP 403"):
            await client.summaries(["76561197960266101"])
        recovered = await client.summaries(["76561197960266101"])
    finally:
        await client.close()

    assert calls == 2
    assert recovered["76561197960266101"].persona_name == "Recovered Player"


@pytest.mark.asyncio
async def test_summaries_share_fresh_cache_and_fall_back_to_stale_snapshot() -> None:
    calls = 0
    now = [0.0]

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls > 1:
            raise httpx.ReadTimeout("temporary outage", request=request)
        return httpx.Response(
            200,
            json={
                "response": {
                    "players": [
                        {
                            "steamid": "76561197960266101",
                            "personaname": "Cached Player",
                            "personastate": 1,
                            "gameid": "730",
                            "gameextrainfo": "Counter-Strike 2",
                        }
                    ]
                }
            },
        )

    client = SteamPresenceClient(
        "a" * 32,
        cache_ttl_seconds=45,
        stale_ttl_seconds=300,
        retry_attempts=1,
        transport=httpx.MockTransport(handler),
        clock=lambda: now[0],
    )
    try:
        fresh = await client.summaries(["76561197960266101"])
        repeated = await client.summaries(["76561197960266101"])
        now[0] = 60
        stale = await client.summaries(["76561197960266101"])
    finally:
        await client.close()

    assert calls == 2
    assert fresh["76561197960266101"].stale is False
    assert repeated["76561197960266101"].stale is False
    assert stale["76561197960266101"].stale is True
    assert "缓存状态" in format_player_game_status(
        "Cached Player",
        stale["76561197960266101"],
    )


@pytest.mark.asyncio
async def test_concurrent_summaries_use_one_upstream_request() -> None:
    calls = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.01)
        return httpx.Response(200, json={"response": {"players": []}})

    client = SteamPresenceClient(
        "a" * 32,
        transport=httpx.MockTransport(handler),
    )
    try:
        await asyncio.gather(
            client.summaries(["76561197960266101"]),
            client.summaries(["76561197960266101"]),
        )
    finally:
        await client.close()

    assert calls == 1


@pytest.mark.asyncio
async def test_failure_cooldown_skips_repeated_unreachable_requests() -> None:
    calls = 0
    now = [0.0]

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("network unavailable", request=request)

    client = SteamPresenceClient(
        "a" * 32,
        retry_attempts=1,
        failure_cooldown_seconds=300,
        transport=httpx.MockTransport(handler),
        clock=lambda: now[0],
    )
    try:
        with pytest.raises(SteamPresenceError, match="查询超时"):
            await client.summaries(["76561197960266101"])
        with pytest.raises(SteamPresenceError, match="暂时不可用"):
            await client.summaries(["76561197960266101"])
        assert calls == 1
        now[0] = 301
        with pytest.raises(SteamPresenceError, match="查询超时"):
            await client.summaries(["76561197960266101"])
    finally:
        await client.close()

    assert calls == 2


@pytest.mark.asyncio
async def test_default_failure_cooldown_retries_after_fifteen_seconds() -> None:
    calls = 0
    now = [0.0]

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ReadTimeout("network unavailable", request=request)
        return httpx.Response(
            200,
            json={
                "response": {
                    "players": [
                        {
                            "steamid": "76561197960266101",
                            "personaname": "Recovered Player",
                            "personastate": 1,
                        }
                    ]
                }
            },
        )

    client = SteamPresenceClient(
        "a" * 32,
        retry_attempts=1,
        transport=httpx.MockTransport(handler),
        clock=lambda: now[0],
    )
    try:
        with pytest.raises(SteamPresenceError, match="查询超时"):
            await client.summaries(["76561197960266101"])
        now[0] = 14
        with pytest.raises(SteamPresenceError, match="暂时不可用"):
            await client.summaries(["76561197960266101"])
        assert calls == 1
        now[0] = 15
        recovered = await client.summaries(["76561197960266101"])
    finally:
        await client.close()

    assert calls == 2
    assert recovered["76561197960266101"].persona_name == "Recovered Player"


@pytest.mark.asyncio
async def test_default_stale_cache_remains_available_for_fifteen_minutes() -> None:
    calls = 0
    now = [0.0]

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls > 1:
            raise httpx.ReadTimeout("temporary outage", request=request)
        return httpx.Response(
            200,
            json={
                "response": {
                    "players": [
                        {
                            "steamid": "76561197960266101",
                            "personaname": "Cached Player",
                            "personastate": 1,
                            "gameid": "730",
                            "gameextrainfo": "Counter-Strike 2",
                        }
                    ]
                }
            },
        )

    client = SteamPresenceClient(
        "a" * 32,
        retry_attempts=1,
        transport=httpx.MockTransport(handler),
        clock=lambda: now[0],
    )
    try:
        await client.summaries(["76561197960266101"])
        now[0] = 600
        stale = await client.summaries(["76561197960266101"])
        now[0] = 901
        with pytest.raises(SteamPresenceError, match="查询超时"):
            await client.summaries(["76561197960266101"])
    finally:
        await client.close()

    assert calls == 3
    assert stale.stale is True
    assert stale["76561197960266101"].stale is True


@pytest.mark.asyncio
async def test_empty_stale_snapshot_keeps_response_level_cache_marker() -> None:
    calls = 0
    now = [0.0]

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls > 1:
            raise httpx.ReadTimeout("temporary outage", request=request)
        return httpx.Response(200, json={"response": {"players": []}})

    client = SteamPresenceClient(
        "a" * 32,
        cache_ttl_seconds=45,
        stale_ttl_seconds=300,
        retry_attempts=1,
        transport=httpx.MockTransport(handler),
        clock=lambda: now[0],
    )
    try:
        assert await client.summaries(["76561197960266101"]) == {}
        now[0] = 60
        stale = await client.summaries(["76561197960266101"])
    finally:
        await client.close()

    assert stale == {}
    assert stale.stale is True
    assert "缓存状态" in format_player_game_status(
        "隐身牛",
        None,
        stale=stale.stale,
    )


@pytest.mark.asyncio
async def test_presence_cache_evicts_oldest_entries_at_capacity() -> None:
    calls = 0
    now = [0.0]

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        steam_id = request.url.params["steamids"]
        return httpx.Response(
            200,
            json={"response": {"players": [{"steamid": steam_id}]}},
        )

    client = SteamPresenceClient(
        "a" * 32,
        cache_ttl_seconds=300,
        stale_ttl_seconds=300,
        max_cache_entries=2,
        transport=httpx.MockTransport(handler),
        clock=lambda: now[0],
    )
    steam_ids = (
        "76561199006406461",
        "76561199006406462",
        "76561199006406463",
    )
    try:
        for index, steam_id in enumerate(steam_ids):
            now[0] = float(index)
            await client.summaries([steam_id])
        now[0] = 3
        await client.summaries([steam_ids[0]])
    finally:
        await client.close()

    assert calls == 4


def test_game_status_formatting_collapses_offline_members() -> None:
    playing = SteamPresence("1", "A", "730", "", "", 1)
    away = SteamPresence("2", "B", "", "", "", 3)
    offline = SteamPresence("3", "C", "", "", "", 0)

    group = format_group_game_status(
        [("牛牛", playing), ("精灵", away), ("隐身牛", offline)],
        total_count=4,
    )

    assert "1/4 人正在游戏" in group
    assert "🎯 牛牛｜Counter-Strike 2" in group
    assert "🟡 精灵｜离开" in group
    assert "离线、隐身或状态不可见：2 人" in group
    assert "隐身牛｜" not in group
    assert format_player_game_status("隐身牛", offline).endswith("状态：离线或隐身")
    assert "资料不可见" in format_player_game_status("未知牛", None)

    cached = SteamPresence("4", "D", "730", "", "Counter-Strike 2", 1, True)
    assert "缓存状态" in format_group_game_status(
        [("缓存牛", cached)],
        total_count=1,
    )


def test_game_status_card_snapshots_keep_visible_rows_and_hidden_count() -> None:
    playing = SteamPresence("1", "A", "730", "", "Counter-Strike 2", 1)
    away = SteamPresence("2", "B", "", "", "", 3)
    offline = SteamPresence("3", "C", "", "", "", 0)

    group = build_group_game_status_snapshot(
        [("牛牛", playing), ("精灵", away), ("隐身牛", offline)],
        total_count=4,
        stale=True,
    )

    assert [entry.name for entry in group.entries] == ["牛牛", "精灵"]
    assert group.entries[0].status_kind == "playing"
    assert group.entries[0].game_name == "Counter-Strike 2"
    assert group.entries[1].status_kind == "away"
    assert group.playing_count == 1
    assert group.hidden_count == 2
    assert group.stale

    hidden_player = build_player_game_status_snapshot("隐身牛", offline)
    assert hidden_player.entries[0].status_kind == "offline"
    assert hidden_player.hidden_count == 1

    missing_player = build_player_game_status_snapshot("未知牛", None)
    assert missing_player.entries[0].status_kind == "unknown"
    assert missing_player.hidden_count == 1
