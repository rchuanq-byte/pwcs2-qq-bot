import json
from urllib.parse import quote

import httpx
import pytest

from pwcs2_bot.stream_live import (
    LivePlatformClient,
    StreamerTarget,
    StreamLiveError,
    format_live_notification,
)


async def _fetch(target: StreamerTarget, payload: object, *, text: str = ""):
    def handler(request: httpx.Request) -> httpx.Response:
        if text:
            return httpx.Response(200, text=text, request=request)
        return httpx.Response(200, json=payload, request=request)

    client = LivePlatformClient(transport=httpx.MockTransport(handler))
    try:
        return await client.fetch(target)
    finally:
        await client.close()


async def test_douyu_live_response_uses_internal_room_id() -> None:
    target = StreamerTarget(
        platform="douyu",
        name="玩机器",
        roomId="6979222",
        url="https://www.douyu.com/6657",
    )
    snapshot = await _fetch(
        target,
        {
            "error": 0,
            "data": {
                "room_id": "6979222",
                "room_status": "1",
                "room_name": "小众宝藏结晶直播间 6657",
                "owner_name": "玩机器丶Machine",
                "cate_name": "CS2",
                "room_thumb": "https://example.test/cover.jpg",
                "start_time": "2026-08-12 16:34:43",
            },
        },
    )
    assert snapshot.is_live is True
    assert snapshot.session_key == "2026-08-12 16:34:43"
    assert snapshot.live_url == "https://www.douyu.com/6657"


@pytest.mark.parametrize("status", ["0", "3", "", None])
async def test_douyu_unknown_status_is_not_treated_as_offline(status) -> None:
    with pytest.raises(StreamLiveError):
        await _fetch(
            StreamerTarget(platform="douyu", name="玩机器", roomId="6979222"),
            {
                "error": 0,
                "data": {"room_id": "6979222", "room_status": status},
            },
        )


async def test_huya_live_response_preserves_large_live_id_as_text() -> None:
    target = StreamerTarget(
        platform="huya",
        name="DANK1NG",
        roomId="10188",
        url="https://www.huya.com/dank1ng",
    )
    api_payload = {
        "status": 200,
        "data": {
            "liveStatus": "ON",
            "realLiveStatus": "ON",
            "profileInfo": {
                "nick": "DANK1NG",
                "profileRoom": 10188,
                "privateHost": "dank1ng",
            },
            "liveData": {
                "startTime": 1786526610,
                "liveId": 7673073361529486879,
                "introduction": "特爱直播",
                "gameFullName": "CS2",
            },
        },
    }
    room_data = {
        "state": "ON",
        "isOn": True,
        "isReplay": False,
        "profileRoom": 10188,
        "privateHost": "dank1ng",
        "nick": "DANK1NG",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "mp.huya.com":
            return httpx.Response(200, json=api_payload, request=request)
        html = (
            "<script>var TT_ROOM_DATA = "
            + json.dumps(room_data)
            + "; var TT_PROFILE_INFO = {};</script>"
        )
        return httpx.Response(200, text=html, request=request)

    client = LivePlatformClient(transport=httpx.MockTransport(handler))
    try:
        snapshot = await client.fetch(target)
    finally:
        await client.close()
    assert snapshot.is_live is True
    assert snapshot.is_replay is False
    assert snapshot.session_key == "7673073361529486879"


async def test_huya_mismatched_alias_is_rejected() -> None:
    with pytest.raises(StreamLiveError):
        await _fetch(
            StreamerTarget(
                platform="huya",
                name="DANK1NG",
                roomId="10188",
                url="https://www.huya.com/dank1ng",
            ),
            {
                "status": 200,
                "data": {
                    "liveStatus": "OFF",
                    "realLiveStatus": "OFF",
                    "profileInfo": {
                        "profileRoom": 10188,
                        "privateHost": "someone-else",
                    },
                    "liveData": [],
                },
            },
        )


async def test_huya_uses_strict_page_fallback_when_json_endpoint_fails() -> None:
    room_data = {
        "state": "ON",
        "isOn": True,
        "isReplay": False,
        "profileRoom": 10188,
        "privateHost": "dank1ng",
        "nick": "DANK1NG",
        "startTime": 1786526610,
        "liveId": "7673073361529486879",
        "introduction": "特爱直播",
        "gameFullName": "CS2",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "mp.huya.com":
            return httpx.Response(403, request=request)
        html = (
            "<script>var TT_ROOM_DATA = "
            + json.dumps(room_data, ensure_ascii=False)
            + "; var TT_PROFILE_INFO = {};</script>"
        )
        return httpx.Response(200, text=html, request=request)

    client = LivePlatformClient(transport=httpx.MockTransport(handler))
    try:
        snapshot = await client.fetch(
            StreamerTarget(
                platform="huya",
                name="DANK1NG",
                roomId="10188",
                url="https://www.huya.com/dank1ng",
            )
        )
    finally:
        await client.close()
    assert snapshot.is_live is True
    assert snapshot.session_key == "7673073361529486879"


async def test_huya_replay_page_is_treated_as_offline() -> None:
    room_data = {
        "state": "ON",
        "isOn": True,
        "isReplay": True,
        "profileRoom": 10188,
        "privateHost": "dank1ng",
        "nick": "DANK1NG",
        "startTime": 1786526610,
        "liveId": "7673073361529486879",
        "introduction": "轮播录像",
        "gameFullName": "CS2",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "mp.huya.com":
            return httpx.Response(403, request=request)
        html = (
            "<script>var TT_ROOM_DATA = "
            + json.dumps(room_data, ensure_ascii=False)
            + "; var TT_PROFILE_INFO = {};</script>"
        )
        return httpx.Response(200, text=html, request=request)

    client = LivePlatformClient(transport=httpx.MockTransport(handler))
    try:
        snapshot = await client.fetch(
            StreamerTarget(
                platform="huya",
                name="DANK1NG",
                roomId="10188",
                url="https://www.huya.com/dank1ng",
            )
        )
    finally:
        await client.close()

    assert snapshot.is_replay is True
    assert snapshot.is_live is False
    assert snapshot.session_key == ""


async def test_douyin_render_data_live_page() -> None:
    render = quote(
        json.dumps(
            {
                "app": {
                    "roomInfo": {
                        "web_rid": "998877",
                        "room": {
                            "status": 2,
                            "id_str": "998877",
                            "title": "夜间排位",
                            "start_time": 123456,
                            "owner": {"nickname": "测试主播"},
                            "cover": {"url_list": ["https://example.test/dy.jpg"]},
                        }
                    }
                }
            },
            ensure_ascii=False,
        )
    )
    html = f'<script id="RENDER_DATA" type="application/json">{render}</script>'
    snapshot = await _fetch(
        StreamerTarget(platform="douyin", name="测试主播", roomId="998877"),
        {},
        text=html,
    )
    assert snapshot.is_live is True
    assert snapshot.streamer_name == "测试主播"


async def test_douyin_react_flight_offline_page() -> None:
    state = [
        "$",
        "$L11",
        None,
        {
            "state": {
                "roomStore": {
                    "roomInfo": {
                        "roomId": "7667574931651218176",
                        "web_rid": "59901412180",
                        "anchor": {
                            "nickname": "MasTurbaT0r",
                            "sec_uid": "MS4wLjABAAAA-test",
                        },
                        "room": {
                            "id_str": "7667574931651218176",
                            "status": 4,
                            "title": "愤怒",
                            "owner": None,
                        },
                    }
                }
            }
        },
    ]
    chunk = "c:" + json.dumps(state, ensure_ascii=False) + "\n"
    html = f"<script>self.__pace_f.push({json.dumps([1, chunk])})</script>"
    snapshot = await _fetch(
        StreamerTarget(
            platform="douyin",
            name="masturbator",
            roomId="59901412180",
            url="https://live.douyin.com/59901412180",
            expectedSecUid="MS4wLjABAAAA-test",
        ),
        {},
        text=html,
    )
    assert snapshot.is_live is False
    assert snapshot.streamer_name == "MasTurbaT0r"
    assert snapshot.title == "愤怒"
    assert snapshot.room_id == "59901412180"


async def test_douyin_react_flight_live_page_builds_session_key() -> None:
    state = {
        "roomInfo": {
            "roomId": "7667574931651218176",
            "web_rid": "59901412180",
            "anchor": {
                "nickname": "MasTurbaT0r",
                "sec_uid": "MS4wLjABAAAA-test",
            },
            "room": {
                "id_str": "7667574931651218176",
                "status": 2,
                "title": "来打 CS2（练枪）",
                "start_time": 1786550400,
                "owner": {"nickname": "MasTurbaT0r"},
            },
        }
    }
    chunk = "c:" + json.dumps(state, ensure_ascii=False) + "\n"
    html = (
        '<script nonce="test">'
        f"self.__pace_f.push({json.dumps([1, chunk])});"
        "</script>"
    )
    snapshot = await _fetch(
        StreamerTarget(
            platform="douyin",
            name="masturbator",
            roomId="59901412180",
            expectedSecUid="MS4wLjABAAAA-test",
        ),
        {},
        text=html,
    )
    assert snapshot.is_live is True
    assert snapshot.streamer_name == "MasTurbaT0r"
    assert snapshot.session_key == "7667574931651218176:1786550400"
    assert snapshot.title == "来打 CS2（练枪）"
    assert snapshot.live_url == "https://live.douyin.com/59901412180"


async def test_douyin_react_flight_mismatched_identity_is_unknown() -> None:
    state = {
        "roomInfo": {
            "web_rid": "59901412180",
            "anchor": {
                "nickname": "别的主播",
                "sec_uid": "MS4wLjABAAAA-other",
            },
            "room": {
                "id_str": "7667574931651218176",
                "status": 4,
                "title": "别的直播间",
            },
        }
    }
    chunk = "c:" + json.dumps(state, ensure_ascii=False) + "\n"
    html = f"<script>self.__pace_f.push({json.dumps([1, chunk])})</script>"
    with pytest.raises(StreamLiveError, match="mismatched streamer identity"):
        await _fetch(
            StreamerTarget(
                platform="douyin",
                name="masturbator",
                roomId="59901412180",
                expectedSecUid="MS4wLjABAAAA-target",
            ),
            {},
            text=html,
        )


async def test_douyin_react_flight_missing_identity_is_unknown() -> None:
    state = {
        "roomInfo": {
            "web_rid": "59901412180",
            "anchor": {"nickname": "MasTurbaT0r"},
            "room": {
                "id_str": "7667574931651218176",
                "status": 4,
                "title": "暂未开播",
            },
        }
    }
    chunk = "c:" + json.dumps(state, ensure_ascii=False) + "\n"
    html = f"<script>self.__pace_f.push({json.dumps([1, chunk])})</script>"
    with pytest.raises(StreamLiveError, match="mismatched streamer identity"):
        await _fetch(
            StreamerTarget(
                platform="douyin",
                name="masturbator",
                roomId="59901412180",
                expectedSecUid="MS4wLjABAAAA-target",
            ),
            {},
            text=html,
        )


async def test_douyin_react_flight_conflicting_identity_is_unknown() -> None:
    state = {
        "roomInfo": {
            "web_rid": "59901412180",
            "anchor": {
                "nickname": "MasTurbaT0r",
                "sec_uid": "MS4wLjABAAAA-target",
            },
            "room": {
                "id_str": "7667574931651218176",
                "status": 2,
                "owner": {
                    "nickname": "别的主播",
                    "sec_uid": "MS4wLjABAAAA-other",
                },
            },
        }
    }
    chunk = "c:" + json.dumps(state, ensure_ascii=False) + "\n"
    html = f"<script>self.__pace_f.push({json.dumps([1, chunk])})</script>"
    with pytest.raises(StreamLiveError, match="mismatched streamer identity"):
        await _fetch(
            StreamerTarget(
                platform="douyin",
                name="masturbator",
                roomId="59901412180",
                expectedSecUid="MS4wLjABAAAA-target",
            ),
            {},
            text=html,
        )


async def test_douyin_react_flight_ignores_recommended_room() -> None:
    state = {
        "recommendations": {
            "roomInfo": {
                "web_rid": "112233",
                "room": {
                    "id_str": "7777777777777777777",
                    "status": 2,
                    "title": "推荐直播间",
                },
            }
        },
        "target": {
            "roomInfo": {
                "web_rid": "59901412180",
                "anchor": {"nickname": "MasTurbaT0r"},
                "room": {
                    "id_str": "7667574931651218176",
                    "status": 4,
                    "title": "目标直播间",
                },
            }
        },
    }
    chunk = "c:" + json.dumps(state, ensure_ascii=False) + "\n"
    html = f"<script>self.__pace_f.push({json.dumps([1, chunk])})</script>"
    snapshot = await _fetch(
        StreamerTarget(
            platform="douyin",
            name="masturbator",
            roomId="59901412180",
        ),
        {},
        text=html,
    )
    assert snapshot.is_live is False
    assert snapshot.title == "目标直播间"


async def test_douyin_react_flight_mismatched_room_is_unknown() -> None:
    state = {
        "roomInfo": {
            "web_rid": "112233",
            "anchor": {"nickname": "别的主播"},
            "room": {
                "id_str": "7777777777777777777",
                "status": 2,
                "title": "别的直播间",
            },
        }
    }
    chunk = "c:" + json.dumps(state, ensure_ascii=False) + "\n"
    html = f"<script>self.__pace_f.push({json.dumps([1, chunk])});</script>"
    with pytest.raises(StreamLiveError):
        await _fetch(
            StreamerTarget(
                platform="douyin",
                name="masturbator",
                roomId="59901412180",
            ),
            {},
            text=html,
        )


async def test_douyin_rejects_recommended_live_room_on_offline_target_page() -> None:
    render = quote(
        json.dumps(
            {
                "app": {
                    "target": {"web_rid": "998877", "status": 4},
                    "recommendations": [
                        {
                            "roomInfo": {
                                "web_rid": "112233",
                                "room": {
                                    "status": 2,
                                    "id_str": "76543210987654321",
                                    "title": "推荐页里的其他直播间",
                                    "owner": {
                                        "nickname": "其他主播",
                                        "web_rid": "112233",
                                    },
                                },
                            }
                        }
                    ],
                }
            },
            ensure_ascii=False,
        )
    )
    html = f'<script id="RENDER_DATA" type="application/json">{render}</script>'
    with pytest.raises(StreamLiveError):
        await _fetch(
            StreamerTarget(platform="douyin", name="测试主播", roomId="998877"),
            {},
            text=html,
        )


async def test_douyin_target_offline_room_wins_over_live_recommendation() -> None:
    render = quote(
        json.dumps(
            {
                "app": {
                    "recommendations": [
                        {
                            "roomInfo": {
                                "web_rid": "112233",
                                "room": {
                                    "status": 2,
                                    "title": "推荐页里的其他直播间",
                                },
                            }
                        }
                    ],
                    "roomInfo": {
                        "web_rid": "998877",
                        "room": {
                            "status": 4,
                            "title": "目标主播的直播间",
                        },
                    },
                }
            },
            ensure_ascii=False,
        )
    )
    html = f'<script id="RENDER_DATA" type="application/json">{render}</script>'
    snapshot = await _fetch(
        StreamerTarget(platform="douyin", name="测试主播", roomId="998877"),
        {},
        text=html,
    )
    assert snapshot.is_live is False
    assert snapshot.title == "目标主播的直播间"


async def test_douyin_rejects_unscoped_status_object_in_page_html() -> None:
    html = (
        '<script type="application/json">'
        '{"status":2,"id_str":"998877","title":"推荐直播"}'
        "</script>"
    )
    with pytest.raises(StreamLiveError):
        await _fetch(
            StreamerTarget(platform="douyin", name="测试主播", roomId="998877"),
            {},
            text=html,
        )


def test_douyin_url_must_match_configured_web_rid() -> None:
    with pytest.raises(ValueError):
        StreamerTarget(
            platform="douyin",
            name="测试主播",
            roomId="998877",
            url="https://live.douyin.com/112233",
        )


@pytest.mark.parametrize(
    "url",
    [
        "https://www.douyu.com/",
        "https://www.douyu.com/directory/all",
        "https://www.huya.com/g/862",
    ],
)
def test_room_url_must_point_directly_to_one_room(url: str) -> None:
    platform = "douyu" if "douyu" in url else "huya"
    with pytest.raises(ValueError):
        StreamerTarget(platform=platform, name="测试主播", roomId="6979222", url=url)


async def test_douyu_without_custom_url_links_to_internal_room() -> None:
    snapshot = await _fetch(
        StreamerTarget(platform="douyu", name="测试主播", roomId="6979222"),
        {
            "error": 0,
            "data": {
                "room_id": "6979222",
                "room_status": "2",
                "room_name": "测试直播间",
                "owner_name": "测试主播",
            },
        },
    )
    assert snapshot.is_live is False
    assert snapshot.live_url == "https://www.douyu.com/6979222"


async def test_douyin_risk_control_page_is_unknown() -> None:
    with pytest.raises(StreamLiveError):
        await _fetch(
            StreamerTarget(platform="douyin", name="测试主播", roomId="998877"),
            {},
            text="<body onload=readygo()></body>",
        )


def test_format_live_notification_sanitizes_newlines() -> None:
    from pwcs2_bot.stream_live import LiveSnapshot

    text = format_live_notification(
        LiveSnapshot(
            platform="douyu",
            room_id="1",
            streamer_name="主播\n名字",
            is_live=True,
            session_key="one",
            title="标题\r\n注入",
            category="CS2",
            cover_url="",
            live_url="https://example.test/live",
            started_at="",
        )
    )
    assert "主播 名字" in text
    assert "标题 注入" in text
