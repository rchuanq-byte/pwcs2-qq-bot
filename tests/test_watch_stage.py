import json

import httpx

from pwcs2_bot.watch_stage import WatchStageClient, format_watch_stage


class FakeWebSocket:
    def __init__(self, messages: list[str]) -> None:
        self.messages = list(messages)
        self.sent: list[str] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args) -> None:
        return None

    async def send(self, message: str) -> None:
        self.sent.append(message)

    async def recv(self) -> str:
        return self.messages.pop(0)


class FakeConnector:
    def __init__(self, sockets: list[FakeWebSocket]) -> None:
        self.sockets = list(sockets)
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, url: str, **kwargs):
        self.calls.append((url, kwargs))
        return self.sockets.pop(0)


async def test_watch_stage_reads_live_scoreboard_and_profiles() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("getWebsocketInfo"):
            return httpx.Response(
                200,
                json={
                    "code": 1,
                    "result": {"websocketUrl": "wss://wss-csgo-pwa.wmpvp.com"},
                },
            )
        return httpx.Response(
            200,
            json={
                "result": {
                    "ctPlayerStatsDTOList": [
                        {
                            "steamId": "76561198000000001",
                            "nickname": "<牛牛>",
                            "pvpScore": 2100,
                            "ratingPro": 1.23,
                            "we": 12.5,
                        }
                    ],
                    "tplayerStatsDTOList": [
                        {
                            "steamId": "76561198000000002",
                            "nickname": "精灵",
                            "pvpScore": 1900,
                            "ratingPro": 1.08,
                        }
                    ],
                }
            },
        )

    match_data = {
        "matchId": "live-match",
        "map": "de_mirage",
        "ctScore": 7,
        "terroristScore": 5,
        "duration": 754,
        "warmUpStatus": False,
        "playerList": [
            {
                "steamId": "76561198000000001",
                "side": "CT",
                "kill": 14,
                "assist": 3,
                "death": 8,
                "score": 32,
                "adr": 91.2,
                "headshot": "57%",
                "alive": True,
            },
            {
                "steamId": "76561198000000002",
                "side": "TERRORIST",
                "kill": 10,
                "assist": 4,
                "death": 10,
                "score": 25,
                "adr": 78.4,
                "headshot": "40%",
                "alive": False,
            },
        ],
    }
    socket = FakeWebSocket(
        [
            "pong",
            json.dumps({"messageType": 10002, "messageData": match_data}),
        ]
    )
    connector = FakeConnector([socket])
    client = WatchStageClient(
        transport=httpx.MockTransport(handler),
        websocket_connect=connector,
    )
    try:
        snapshot = await client.snapshot("76561198000000001")
    finally:
        await client.close()

    assert snapshot.status == "running"
    assert snapshot.map_name == "de_mirage"
    assert snapshot.ct_score == 7
    assert snapshot.terrorist_score == 5
    assert snapshot.duration == "12:34"
    assert snapshot.players[0].nickname == "<牛牛>"
    assert snapshot.players[0].pvp_score == 2100
    assert snapshot.players[0].avg_rating == 1.23
    assert socket.sent[0] == "ping"
    assert json.loads(socket.sent[1])["messageData"]["steam_id"] == "76561198000000001"
    assert requests[0].url.params["platform"] == "2"
    assert requests[1].url.path.endswith("getPvPMatchTeamStatisticsData")
    assert connector.calls[0][1]["origin"] == "https://news.wmpvp.com"

    text = format_watch_stage(snapshot)
    assert "荒漠迷城 · de_mirage" in text
    assert "CT 7 : 5 T" in text
    assert "<牛牛> 14/3/8" in text


async def test_watch_stage_falls_back_across_platforms_when_no_match() -> None:
    seen_platforms: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_platforms.append(request.url.params["platform"])
        return httpx.Response(
            200,
            json={
                "code": 1,
                "result": {"websocketUrl": "wss://wss-csgo-pwa.wmpvp.com"},
            },
        )

    no_active = json.dumps(
        {"messageType": 10003, "messageData": {"error": "match is over"}}
    )
    connector = FakeConnector(
        [
            FakeWebSocket(["pong", no_active]),
            FakeWebSocket(["pong", no_active]),
        ]
    )
    client = WatchStageClient(
        transport=httpx.MockTransport(handler),
        websocket_connect=connector,
    )
    try:
        snapshot = await client.snapshot("76561198000000001")
    finally:
        await client.close()

    assert snapshot.status == "no_active_match"
    assert seen_platforms == ["2", "1"]
    assert "当前没有可展示的实时比赛" in format_watch_stage(snapshot)


async def test_watch_stage_internal_forced_platform_does_not_cross_source_fallback() -> None:
    seen_platforms: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_platforms.append(request.url.params["platform"])
        return httpx.Response(
            200,
            json={
                "code": 1,
                "result": {"websocketUrl": "wss://wss-csgo-pwa.wmpvp.com"},
            },
        )

    no_active = json.dumps(
        {"messageType": 10003, "messageData": {"error": "match is over"}}
    )
    connector = FakeConnector([FakeWebSocket(["pong", no_active])])
    client = WatchStageClient(
        transport=httpx.MockTransport(handler),
        websocket_connect=connector,
    )
    try:
        snapshot = await client.snapshot(
            "76561198000000001",
            platform="1",
        )
    finally:
        await client.close()

    assert snapshot.status == "no_active_match"
    assert snapshot.platform == "1"
    assert seen_platforms == ["1"]
    assert len(connector.calls) == 1
    assert format_watch_stage(snapshot).startswith(
        "👁️ 官匹观战｜76561198000000001"
    )


async def test_watch_stage_falls_back_to_official_match_and_profiles() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("getWebsocketInfo"):
            return httpx.Response(
                200,
                json={
                    "code": 1,
                    "result": {
                        "websocketUrl": "wss://wss-csgo-pwa.wmpvp.com"
                    },
                },
            )
        return httpx.Response(
            200,
            json={
                "result": {
                    "ctTeamPlayInfoList": [
                        {
                            "steamId": "76561198000000001",
                            "nickname": "官匹牛牛",
                            "rating": 1.32,
                            "ratingPro": 0,
                            "pvpScore": 0,
                            "we": 0,
                        }
                    ],
                    "teTeamPlayInfoList": [],
                }
            },
        )

    no_active = json.dumps({"messageType": 10003, "messageData": {}})
    official = json.dumps(
        {
            "messageType": 10002,
            "messageData": {
                "matchId": "1.2.3.4:27015",
                "map": "de_inferno",
                "ctScore": 1,
                "terroristScore": 0,
                "duration": 50,
                "warmUpStatus": False,
                "playerList": [
                    {
                        "steamId": "76561198000000001",
                        "side": "CT",
                        "kill": 2,
                        "assist": 0,
                        "death": 0,
                        "score": 4,
                        "adr": 120,
                        "alive": True,
                    }
                ],
            },
        }
    )
    connector = FakeConnector(
        [
            FakeWebSocket(["pong", no_active]),
            FakeWebSocket(["pong", official]),
        ]
    )
    client = WatchStageClient(
        transport=httpx.MockTransport(handler),
        websocket_connect=connector,
    )
    try:
        snapshot = await client.snapshot("76561198000000001")
    finally:
        await client.close()

    assert snapshot.status == "running"
    assert snapshot.platform == "1"
    assert snapshot.match_id == "1.2.3.4:27015"
    assert snapshot.players[0].nickname == "官匹牛牛"
    assert snapshot.players[0].avg_rating == 1.32
    assert snapshot.players[0].pvp_score is None
    assert snapshot.players[0].avg_we is None
    assert "官匹观战" in format_watch_stage(snapshot)
    assert [
        request.url.params["platform"]
        for request in requests
        if request.url.path.endswith("getWebsocketInfo")
    ] == ["2", "1"]
    assert requests[-1].url.path.endswith("getMatchTeamStatisticsData")


async def test_watch_stage_skips_unrelated_platform_two_snapshot() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("getWebsocketInfo"):
            return httpx.Response(
                200,
                json={
                    "code": 1,
                    "result": {
                        "websocketUrl": "wss://wss-csgo-pwa.wmpvp.com"
                    },
                },
            )
        return httpx.Response(200, json={"result": {}})

    unrelated = json.dumps(
        {
            "messageType": 10002,
            "messageData": {
                "matchId": "PVP@stale",
                "map": "de_mirage",
                "playerList": [
                    {
                        "steamId": "76561198000000999",
                        "side": "CT",
                    }
                ],
            },
        }
    )
    official = json.dumps(
        {
            "messageType": 10002,
            "messageData": {
                "matchId": "1.2.3.4:27015",
                "map": "de_nuke",
                "playerList": [
                    {
                        "steamId": "76561198000000001",
                        "side": "CT",
                    }
                ],
            },
        }
    )
    connector = FakeConnector(
        [
            FakeWebSocket(
                [
                    "pong",
                    unrelated,
                    json.dumps({"messageType": 10003, "messageData": {}}),
                ]
            ),
            FakeWebSocket(["pong", official]),
        ]
    )
    client = WatchStageClient(
        transport=httpx.MockTransport(handler),
        websocket_connect=connector,
    )
    try:
        snapshot = await client.snapshot("76561198000000001")
    finally:
        await client.close()

    assert snapshot.platform == "1"
    assert snapshot.match_id == "1.2.3.4:27015"


async def test_watch_stage_waits_for_target_after_unrelated_message() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "code": 1,
                "result": {"websocketUrl": "wss://wss-csgo-pwa.wmpvp.com"},
            },
        )

    def match_message(match_id: str, steam_id: str) -> str:
        return json.dumps(
            {
                "messageType": 10002,
                "messageData": {
                    "matchId": match_id,
                    "map": "de_mirage",
                    "playerList": [
                        {
                            "steamId": steam_id,
                            "side": "CT",
                        }
                    ],
                },
            }
        )

    target_steam_id = "76561198000000001"
    connector = FakeConnector(
        [
            FakeWebSocket(
                [
                    "pong",
                    match_message("PVP@stale", "76561198000000999"),
                    match_message("PVP@target", target_steam_id),
                ]
            )
        ]
    )
    client = WatchStageClient(
        transport=httpx.MockTransport(handler),
        websocket_connect=connector,
    )
    try:
        snapshot = await client.snapshot(target_steam_id)
    finally:
        await client.close()

    assert snapshot.status == "running"
    assert snapshot.platform == "2"
    assert snapshot.match_id == "PVP@target"
    assert snapshot.players[0].steam_id == target_steam_id
    assert len(connector.calls) == 1


async def test_watch_stage_never_returns_unrelated_matches_from_both_platforms() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("getWebsocketInfo"):
            return httpx.Response(
                200,
                json={
                    "code": 1,
                    "result": {
                        "websocketUrl": "wss://wss-csgo-pwa.wmpvp.com"
                    },
                },
            )
        return httpx.Response(200, json={"result": {}})

    def unrelated(match_id: str) -> str:
        return json.dumps(
            {
                "messageType": 10002,
                "messageData": {
                    "matchId": match_id,
                    "map": "de_mirage",
                    "playerList": [
                        {
                            "steamId": "76561198000000999",
                            "side": "CT",
                        }
                    ],
                },
            }
        )

    connector = FakeConnector(
        [
            FakeWebSocket(
                [
                    "pong",
                    unrelated("PVP@stale"),
                    json.dumps({"messageType": 10003, "messageData": {}}),
                ]
            ),
            FakeWebSocket(
                [
                    "pong",
                    unrelated("official-stale"),
                    json.dumps({"messageType": 10003, "messageData": {}}),
                ]
            ),
        ]
    )
    client = WatchStageClient(
        transport=httpx.MockTransport(handler),
        websocket_connect=connector,
    )
    try:
        snapshot = await client.snapshot("76561198000000001")
    finally:
        await client.close()

    assert snapshot.status == "no_active_match"
    assert snapshot.requested_steam_id == "76561198000000001"
    assert snapshot.match_id == ""


async def test_watch_stage_does_not_return_unrelated_match_when_other_platform_fails() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("platform") == "1":
            return httpx.Response(503)
        if request.url.path.endswith("getWebsocketInfo"):
            return httpx.Response(
                200,
                json={
                    "code": 1,
                    "result": {
                        "websocketUrl": "wss://wss-csgo-pwa.wmpvp.com"
                    },
                },
            )
        return httpx.Response(200, json={"result": {}})

    unrelated = json.dumps(
        {
            "messageType": 10002,
            "messageData": {
                "matchId": "PVP@stale",
                "map": "de_mirage",
                "playerList": [
                    {
                        "steamId": "76561198000000999",
                        "side": "CT",
                    }
                ],
            },
        }
    )
    client = WatchStageClient(
        transport=httpx.MockTransport(handler),
        websocket_connect=FakeConnector(
            [
                FakeWebSocket(
                    [
                        "pong",
                        unrelated,
                        json.dumps({"messageType": 10003, "messageData": {}}),
                    ]
                )
            ]
        ),
    )
    try:
        snapshot = await client.snapshot("76561198000000001")
    finally:
        await client.close()

    assert snapshot.status == "no_active_match"
    assert snapshot.match_id == ""
