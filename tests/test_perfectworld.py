import json

import httpx
import pytest

from pwcs2_bot.perfectworld import PerfectWorldApiError, PerfectWorldClient


async def test_detail_stats_request_contract_and_cache() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "statusCode": 0,
                "errorMessage": "",
                "data": {"name": "Player", "steamId": "76561198929215155"},
            },
        )

    client = PerfectWorldClient(
        "secret-token",
        base_url="https://example.test",
        my_steam_id="76561198000000000",
        transport=httpx.MockTransport(handler),
        cache_ttl_seconds=60,
    )
    try:
        first = await client.detail_stats("76561198929215155")
        second = await client.detail_stats("76561198929215155")
    finally:
        await client.close()

    assert first == second
    assert len(requests) == 1
    request = requests[0]
    assert request.url.path == "/api/csgo/home/pvp/detailStats"
    assert request.headers["token"] == "secret-token"
    assert request.headers["appversion"] == "3.5.4.172"
    assert json.loads(request.content) == {
        "accessToken": "",
        "mySteamId": 76561198000000000,
        "toSteamId": 76561198929215155,
    }


async def test_detail_stats_falls_back_to_target_as_my_steam_id() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "statusCode": 0,
                "errorMessage": "",
                "data": {"name": "Player", "steamId": "76561198929215155"},
            },
        )

    client = PerfectWorldClient(
        "secret-token",
        base_url="https://example.test",
        transport=httpx.MockTransport(handler),
    )
    try:
        await client.detail_stats("76561198929215155")
    finally:
        await client.close()

    assert json.loads(requests[0].content)["mySteamId"] == 76561198929215155


async def test_detail_stats_v2_request_contract_and_cache() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "statusCode": 0,
                "data": {
                    "name": "Player",
                    "steamId": "76561198929215155",
                    "radar": {
                        "marksmanship": {
                            "detail": {"rapid_stop_rate_raw": "0.830"}
                        }
                    },
                },
            },
        )

    client = PerfectWorldClient(
        "secret-token",
        base_url="https://example.test",
        my_steam_id="76561198000000000",
        transport=httpx.MockTransport(handler),
    )
    try:
        first = await client.detail_stats_v2("76561198929215155")
        second = await client.detail_stats_v2("76561198929215155")
    finally:
        await client.close()

    assert first == second
    assert len(requests) == 1
    request = requests[0]
    assert request.url.path == "/api/csgo/home/pvp/detailStats/v2"
    assert request.headers["token"] == "secret-token"
    assert json.loads(request.content) == {
        "accessToken": "",
        "mySteamId": 76561198000000000,
        "toSteamId": 76561198929215155,
    }


async def test_match_list_request_contract() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "statusCode": 0,
                "data": {"matchList": [{"matchId": "PVP@test"}]},
            },
        )

    client = PerfectWorldClient(
        "secret-token",
        base_url="https://example.test",
        my_steam_id="76561198000000000",
        transport=httpx.MockTransport(handler),
    )
    try:
        matches = await client.match_list("76561198929215155", page_size=7)
    finally:
        await client.close()

    assert matches == [{"matchId": "PVP@test"}]
    request = requests[0]
    assert request.url.path == "/api/csgo/home/match/list"
    assert json.loads(request.content) == {
        "csgoSeasonId": "recent",
        "dataSource": 3,
        "mySteamId": 76561198000000000,
        "page": 1,
        "pageSize": 7,
        "pvpType": -1,
        "toSteamId": 76561198929215155,
    }


async def test_official_match_list_uses_source_one_and_normalizes_items() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "statusCode": 0,
                "data": {
                    "matchList": [
                        {"matchId": "official-1"},
                        {"matchId": "official-2", "dataSource": 3},
                    ]
                },
            },
        )

    client = PerfectWorldClient(
        "secret-token",
        base_url="https://example.test",
        my_steam_id="76561198000000000",
        transport=httpx.MockTransport(handler),
    )
    try:
        matches = await client.official_match_list(
            "76561198929215155",
            page_size=7,
        )
    finally:
        await client.close()

    assert matches == [
        {"matchId": "official-1", "dataSource": 1},
        {"matchId": "official-2", "dataSource": 1},
    ]
    assert json.loads(requests[0].content) == {
        "csgoSeasonId": "recent",
        "dataSource": 1,
        "mySteamId": 76561198000000000,
        "page": 1,
        "pageSize": 7,
        "pvpType": -1,
        "toSteamId": 76561198929215155,
    }


async def test_match_detail_request_contract_and_cache() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "code": 0,
                "message": "",
                "data": {
                    "base": {"matchId": "PVP@test", "score1": 13, "score2": 9},
                    "players": [{"playerId": str(index), "team": index // 5 + 1}
                                for index in range(10)],
                },
            },
        )

    client = PerfectWorldClient(
        "secret-token",
        base_url="https://example.test",
        transport=httpx.MockTransport(handler),
    )
    try:
        first = await client.match_detail("PVP@test")
        second = await client.match_detail("PVP@test")
    finally:
        await client.close()

    assert first == second
    assert len(first["players"]) == 10
    assert len(requests) == 1
    request = requests[0]
    assert request.url.path == "/api/v1/csgo/match"
    assert request.headers["accesstoken"] == "secret-token"
    assert request.headers["platform"] == "h5_android"
    assert json.loads(request.content) == {
        "matchId": "PVP@test",
        "platform": "admin",
        "dataSource": 3,
    }


async def test_match_detail_cache_isolated_by_data_source() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        body = json.loads(request.content)
        data_source = int(body["dataSource"])
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "base": {
                        "matchId": "shared-id",
                        "dataSource": data_source,
                    },
                    "players": [
                        {"playerId": str(index)}
                        for index in range(10)
                    ],
                },
            },
        )

    client = PerfectWorldClient(
        "secret-token",
        base_url="https://example.test",
        transport=httpx.MockTransport(handler),
    )
    try:
        official = await client.match_detail("shared-id", data_source=1)
        platform = await client.match_detail("shared-id", data_source=3)
        official_cached = await client.match_detail("shared-id", data_source=1)
    finally:
        await client.close()

    assert official["base"]["dataSource"] == 1
    assert platform["base"]["dataSource"] == 3
    assert official_cached is official
    assert [json.loads(request.content)["dataSource"] for request in requests] == [1, 3]


async def test_unexpanded_my_steam_id_placeholder_is_treated_as_blank() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "statusCode": 0,
                "errorMessage": "",
                "data": {"name": "Player", "steamId": "76561198929215155"},
            },
        )

    client = PerfectWorldClient(
        "secret-token",
        base_url="https://example.test",
        my_steam_id="${PERFECTWORLD_MY_STEAM_ID}",
        transport=httpx.MockTransport(handler),
    )
    try:
        await client.detail_stats("76561198929215155")
    finally:
        await client.close()

    assert json.loads(requests[0].content)["mySteamId"] == 76561198929215155


async def test_api_status_error_is_user_displayable() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"statusCode": 401, "errorMessage": "token expired"})

    client = PerfectWorldClient(
        "expired",
        base_url="https://example.test",
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(PerfectWorldApiError, match="token expired"):
            await client.detail_stats("76561198929215155")
    finally:
        await client.close()


async def test_empty_token_fails_before_network_call() -> None:
    client = PerfectWorldClient("")
    try:
        with pytest.raises(PerfectWorldApiError, match="PERFECTWORLD_TOKEN"):
            await client.detail_stats("76561198929215155")
    finally:
        await client.close()
