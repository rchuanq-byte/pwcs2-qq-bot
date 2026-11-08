from pwcs2_bot.teamspeak import (
    TeamSpeakChannel,
    TeamSpeakMember,
    TeamSpeakStatus,
    _parse_records,
    format_teamspeak_status,
)


def test_parse_records_decodes_teamspeak_escapes() -> None:
    records = _parse_records(
        ["cid=1 channel_name=General\\sLobby|cid=2 channel_name=Music\\pAFK"]
    )

    assert records == [
        {"cid": "1", "channel_name": "General Lobby"},
        {"cid": "2", "channel_name": "Music|AFK"},
    ]


def test_format_status_groups_members_and_marks_away() -> None:
    status = TeamSpeakStatus(
        server_name="Test",
        max_clients=32,
        channels=(
            TeamSpeakChannel(
                channel_id=1,
                name="竞技一队",
                members=(
                    TeamSpeakMember("Alice"),
                    TeamSpeakMember("Bob", away=True),
                ),
            ),
            TeamSpeakChannel(channel_id=2, name="空频道"),
        ),
    )

    result = format_teamspeak_status(status, address="ts.example.com")

    assert result.startswith("TS3 实时状态｜在线 2/32")
    assert "竞技一队（2人）" in result
    assert "Alice、Bob〔离开〕" in result
    assert "空频道" not in result


def test_format_status_handles_empty_server() -> None:
    status = TeamSpeakStatus(server_name="Test", max_clients=32, channels=())

    result = format_teamspeak_status(status, address="ts.example.com")

    assert "在线 0/32" in result
    assert result.endswith("当前没有成员在线")
