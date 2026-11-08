from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from nanobot.bus.queue import MessageBus

from pwcs2_bot.channel import PWCS2Channel, PWCS2Config, _highlight_caption
from pwcs2_bot.database import BindingRepository
from pwcs2_bot.demo_highlights import (
    DemoHighlightAnalyzer,
    DemoKillEvent,
    DemoMatchTimeline,
    DemoRound,
)
from pwcs2_bot.demo_source import DemoRequest, DemoSourceManager, PwaDemoProvider
from pwcs2_bot.highlight_controller import (
    HighlightController,
    _highlight_label,
    _remove_managed_demo,
)
from pwcs2_bot.highlight_jobs import HighlightJobStore
from pwcs2_bot.match_formatting import build_match_snapshot

_ACCOUNT_STEAM_ID = "76561197960266101"
_MEMBER_STEAM_ID = "76561197960266104"


class _Parser:
    def parse(self, _path: Path) -> DemoMatchTimeline:
        return DemoMatchTimeline(
            match_id="from-demo-header",
            map_name="de_dust2",
            tick_rate=64,
            kills=tuple(
                DemoKillEvent(
                    tick=640 + index * 64,
                    time_seconds=10 + index,
                    round_number=3,
                    attacker_steam_id=_MEMBER_STEAM_ID,
                    victim_steam_id=f"7656119800000000{index}",
                    weapon="ak47",
                    headshot=index < 2,
                    attacker_team="T",
                    victim_team="CT",
                )
                for index in range(3)
            ),
            rounds=(DemoRound(3, start_seconds=5, end_seconds=25, winner_team="T"),),
        )


def test_lowlight_label_and_delivery_caption_are_explicit() -> None:
    timeline = DemoMatchTimeline(
        match_id="lowlight-label",
        map_name="de_nuke",
        tick_rate=64,
        kills=(
            DemoKillEvent(
                tick=6400,
                time_seconds=100,
                round_number=5,
                attacker_steam_id="",
                victim_steam_id=_MEMBER_STEAM_ID,
                weapon="world",
                headshot=False,
            ),
        ),
    )
    window = DemoHighlightAnalyzer().analyze(
        timeline,
        tracked_steam_ids=(_MEMBER_STEAM_ID,),
    )[0]
    item = SimpleNamespace(
        steam_id=_MEMBER_STEAM_ID,
        match_id="PVP@lowlight",
        context={"highlight": window.to_payload()},
    )

    assert _highlight_label(window) == "下饭时刻 · 离谱暴毙"
    assert _highlight_caption(item) == (
        "🍚 下饭时刻｜离谱暴毙｜SteamID "
        f"{_MEMBER_STEAM_ID}｜比赛 PVP@lowlight"
    )


@pytest.mark.parametrize("reported_patch,demo_patch", [
    ("", ""), ("14181", "14181"), ("14180", "14181"),
])
async def test_controller_bridges_durable_intent_to_windows_job(
    tmp_path: Path, reported_patch: str, demo_patch: str,
) -> None:
    async def fetch(_request: DemoRequest, staging: Path) -> Path:
        demo = staging / "download.dem"
        demo.write_bytes(b"PBDEMS2\x00" + b"demo" * 32)
        return demo

    repository = BindingRepository(tmp_path / "bindings.db")
    job, created = repository.enqueue_highlight_render_job(
        "987654",
        "PVP@integration",
        _ACCOUNT_STEAM_ID,
        {
            "schema_version": 1,
            "kind": "demo_match",
            "highlight_id": "intent-1",
            "source": "perfectworld",
            "tracked_steam_ids": [_MEMBER_STEAM_ID],
            "match": {"map_name": "de_dust2"},
        },
        dedupe_key="demo-match:987654:pvp:PVP@integration",
    )
    assert created is True
    demo_manager = DemoSourceManager(
        tmp_path / "managed-demos",
        tmp_path / "manual-import",
        providers=(PwaDemoProvider(fetch),),
    )
    worker_store = HighlightJobStore(tmp_path / "highlights")
    assert worker_store.claim("pc-test", renderer_patch=reported_patch) is None
    controller = HighlightController(
        repository,
        demo_manager,
        worker_store,
        parser=_Parser(),  # type: ignore[arg-type]
        analyzer=DemoHighlightAnalyzer(),
        initial_delay_seconds=0,
        renderer_patch_version="14180" if reported_patch else "",
        demo_patch_reader=lambda _path: demo_patch,
    )

    assert await controller.run_once() == 1

    completed = repository.get_highlight_render_job(job.dedupe_key)
    assert completed is not None
    assert completed.status == "completed"
    assert completed.output is not None
    assert completed.output["status"] == "queued_for_windows"
    claimed = worker_store.claim("pc-test")
    assert claimed is not None
    assert claimed.match_id == "PVP@integration"
    assert claimed.steam_id == _MEMBER_STEAM_ID
    assert claimed.segments[0].label.startswith("三杀")
    assert claimed.context["highlight"]["stats"]["kills"] == 3
    assert claimed.demo_patch == demo_patch
    assert not tuple((tmp_path / "managed-demos").rglob("*.dem"))


async def test_controller_skips_demo_for_a_different_windows_cs2_patch(
    tmp_path: Path,
) -> None:
    async def fetch(_request: DemoRequest, staging: Path) -> Path:
        demo = staging / "download.dem"
        demo.write_bytes(b"PBDEMS2\x00" + b"demo" * 32)
        return demo

    repository = BindingRepository(tmp_path / "bindings.db")
    job, created = repository.enqueue_highlight_render_job(
        "987654",
        "PVP@stale-demo",
        _ACCOUNT_STEAM_ID,
        {
            "schema_version": 1,
            "kind": "demo_match",
            "highlight_id": "intent-stale-demo",
            "source": "perfectworld",
            "tracked_steam_ids": [_MEMBER_STEAM_ID],
        },
        dedupe_key="demo-match:987654:pvp:PVP@stale-demo",
    )
    demo_manager = DemoSourceManager(
        tmp_path / "managed-demos",
        tmp_path / "manual-import",
        providers=(PwaDemoProvider(fetch),),
    )
    worker_store = HighlightJobStore(tmp_path / "highlights")
    controller = HighlightController(
        repository,
        demo_manager,
        worker_store,
        parser=_Parser(),  # type: ignore[arg-type]
        analyzer=DemoHighlightAnalyzer(),
        initial_delay_seconds=0,
        renderer_patch_version="1.41.8.0",
        demo_patch_reader=lambda _path: "1.41.7.8",
    )

    assert await controller.run_once() == 1

    completed = repository.get_highlight_render_job(job.dedupe_key)
    assert completed is not None
    assert completed.status == "completed"
    assert completed.output == {
        "status": "skipped_renderer_version",
        "demo_patch": "14178",
        "renderer_patch": "14180",
    }
    assert worker_store.claim("pc-test") is None
    assert not tuple((tmp_path / "managed-demos").rglob("*.dem"))


async def test_controller_does_not_download_when_windows_backlog_is_full(
    tmp_path: Path,
) -> None:
    fetch_calls: list[str] = []

    async def fetch(request: DemoRequest, staging: Path) -> Path:
        fetch_calls.append(request.match_id)
        demo = staging / "download.dem"
        demo.write_bytes(b"PBDEMS2\x00" + b"demo" * 32)
        return demo

    repository = BindingRepository(tmp_path / "bindings.db")
    queued, _created = repository.enqueue_highlight_render_job(
        "987654",
        "PVP@backpressure",
        _ACCOUNT_STEAM_ID,
        {
            "schema_version": 1,
            "kind": "demo_match",
            "highlight_id": "intent-backpressure",
            "source": "perfectworld",
            "tracked_steam_ids": [_MEMBER_STEAM_ID],
        },
        dedupe_key="demo-match:987654:pvp:PVP@backpressure",
    )
    demo_manager = DemoSourceManager(
        tmp_path / "managed-demos",
        tmp_path / "manual-import",
        providers=(PwaDemoProvider(fetch),),
    )
    worker_store = HighlightJobStore(tmp_path / "highlights")
    (worker_store.demo_root / "queued-a.dem").write_bytes(b"a")
    (worker_store.demo_root / "queued-b.dem").write_bytes(b"b")
    controller = HighlightController(
        repository,
        demo_manager,
        worker_store,
        parser=_Parser(),  # type: ignore[arg-type]
        analyzer=DemoHighlightAnalyzer(),
        initial_delay_seconds=0,
        max_backlog_demos=2,
    )

    assert await controller.run_once() == 0
    assert fetch_calls == []
    persisted = repository.get_highlight_render_job(queued.dedupe_key)
    assert persisted is not None
    assert persisted.status == "pending"
    assert persisted.attempt_count == 0


async def test_controller_counts_abandoned_source_bytes_before_download(
    tmp_path: Path,
) -> None:
    fetch_calls: list[str] = []

    async def fetch(request: DemoRequest, staging: Path) -> Path:
        fetch_calls.append(request.match_id)
        demo = staging / "download.dem"
        demo.write_bytes(b"PBDEMS2\x00" + b"demo" * 32)
        return demo

    repository = BindingRepository(tmp_path / "bindings.db")
    queued, _created = repository.enqueue_highlight_render_job(
        "987654",
        "PVP@source-backpressure",
        _ACCOUNT_STEAM_ID,
        {
            "schema_version": 1,
            "kind": "demo_match",
            "highlight_id": "intent-source-backpressure",
            "source": "perfectworld",
            "tracked_steam_ids": [_MEMBER_STEAM_ID],
        },
        dedupe_key="demo-match:987654:pvp:PVP@source-backpressure",
    )
    demo_manager = DemoSourceManager(
        tmp_path / "managed-demos",
        tmp_path / "manual-import",
        providers=(PwaDemoProvider(fetch),),
    )
    abandoned = demo_manager.storage_path / ".staging" / ("a" * 32)
    abandoned.mkdir(parents=True)
    (abandoned / ".partial.download").write_bytes(b"x" * 20)
    controller = HighlightController(
        repository,
        demo_manager,
        HighlightJobStore(tmp_path / "highlights"),
        parser=_Parser(),  # type: ignore[arg-type]
        analyzer=DemoHighlightAnalyzer(),
        initial_delay_seconds=0,
        max_backlog_demo_bytes=32,
        acquisition_reservation_bytes=16,
    )

    assert await controller.run_once() == 0
    assert fetch_calls == []
    persisted = repository.get_highlight_render_job(queued.dedupe_key)
    assert persisted is not None
    assert persisted.status == "pending"
    assert persisted.attempt_count == 0


def test_managed_demo_cleanup_refuses_symlink_and_unmanaged_name(tmp_path: Path) -> None:
    storage = tmp_path / "managed-demos"
    platform = storage / "perfectworld"
    platform.mkdir(parents=True)
    target = platform / ("a" * 16 + "-" + "b" * 16 + ".dem")
    target.write_bytes(b"PBDEMS2\x00demo")
    link = platform / ("c" * 16 + "-" + "d" * 16 + ".dem")
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symbolic links are not available on this test host")
    unmanaged = platform / "manual.dem"
    unmanaged.write_bytes(b"PBDEMS2\x00manual")

    _remove_managed_demo(link, storage)
    _remove_managed_demo(unmanaged, storage)

    assert link.is_symlink()
    assert target.is_file()
    assert unmanaged.is_file()


async def test_match_notification_intent_is_idempotent_and_perfect_only(
    tmp_path: Path,
) -> None:
    channel = PWCS2Channel(
        {
            "enabled": True,
            "allowFrom": ["*"],
            "pwToken": "pw-secret",
            "pwMySteamId": _ACCOUNT_STEAM_ID,
            "databasePath": str(tmp_path / "bindings.db"),
            "highlightEnabled": True,
            "highlightDataPath": str(tmp_path / "highlights"),
            "highlightIntentDatabasePath": str(tmp_path / "highlights" / "intents.db"),
            "statsImageEnabled": False,
        },
        MessageBus(),
    )
    detail = {
        "base": {
            "matchId": "PVP@match",
            "map": "de_dust2",
            "score1": 13,
            "score2": 9,
            "winTeam": 1,
        },
        "players": [
            {
                "playerId": _ACCOUNT_STEAM_ID,
                "nickName": "Account",
                "team": 1,
                "kill": 15,
                "death": 12,
            },
            {
                "playerId": _MEMBER_STEAM_ID,
                "nickName": "Member",
                "team": 1,
                "kill": 24,
                "death": 10,
            },
        ],
    }
    perfect = build_match_snapshot(
        {"matchId": "PVP@match", "team": 1, "winTeam": 1},
        steam_id=_MEMBER_STEAM_ID,
        detail=detail,
        group_member_steam_ids={_ACCOUNT_STEAM_ID, _MEMBER_STEAM_ID},
    )
    official = build_match_snapshot(
        {"matchId": "official", "dataSource": 1},
        steam_id=_ACCOUNT_STEAM_ID,
        detail={"base": {"dataSource": 1, "matchId": "official"}},
    )
    try:
        await channel._enqueue_match_highlight("987654", perfect)
        await channel._enqueue_match_highlight("987654", perfect)
        await channel._enqueue_match_highlight("987654", official)
    finally:
        await channel.stop()

    repository = BindingRepository(tmp_path / "highlights" / "intents.db")
    queued = repository.get_highlight_render_job(
        "demo-match:987654:pvp:PVP@match"
    )
    assert queued is not None
    assert queued.steam_id == _MEMBER_STEAM_ID
    assert queued.payload["tracked_steam_ids"] == [
        _ACCOUNT_STEAM_ID,
        _MEMBER_STEAM_ID,
    ]
    with repository._connect() as connection:  # noqa: SLF001 - integration assertion
        count = connection.execute("SELECT COUNT(*) FROM highlight_render_jobs").fetchone()[0]
    assert count == 1


async def test_match_notification_retries_intent_before_sending(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    channel = PWCS2Channel(
        {
            "enabled": True,
            "allowFrom": ["*"],
            "pwToken": "pw-secret",
            "databasePath": str(tmp_path / "bindings.db"),
            "highlightEnabled": True,
            "highlightDataPath": str(tmp_path / "highlights"),
            "highlightIntentDatabasePath": str(tmp_path / "highlights" / "intents.db"),
            "statsImageEnabled": False,
        },
        MessageBus(),
    )
    attempts: list[int] = []
    replies: list[str] = []

    async def fail_enqueue(_group_id: str, _match: object) -> None:
        attempts.append(1)
        raise OSError("database busy")

    async def record_reply(
        _kind: str,
        _target: str,
        content: str,
        **_kwargs: object,
    ) -> None:
        replies.append(content)

    async def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(channel, "_enqueue_match_highlight", fail_enqueue)
    monkeypatch.setattr(channel, "_reply", record_reply)
    monkeypatch.setattr("pwcs2_bot.channel.asyncio.sleep", no_sleep)
    match = build_match_snapshot(
        {"matchId": "PVP@retry", "team": 1, "winTeam": 1},
        steam_id=_ACCOUNT_STEAM_ID,
        detail={
            "base": {"matchId": "PVP@retry", "team": 1, "winTeam": 1},
            "players": [],
        },
    )
    try:
        with pytest.raises(OSError, match="database busy"):
            await channel._send_match_notification("987654", match)
    finally:
        await channel.stop()

    assert len(attempts) == 3
    assert replies == []


def test_highlight_config_defaults_and_aliases() -> None:
    defaults = PWCS2Config()
    assert defaults.highlight_enabled is False
    assert defaults.highlight_data_path == "/data/highlights"
    assert defaults.highlight_intent_database_path == "/data/highlights/intents.db"
    assert defaults.highlight_delivery_timeout_seconds == 120
    assert defaults.highlight_unknown_retention_days == 14
    configured = PWCS2Config.model_validate(
        {
            "highlightEnabled": True,
            "highlightDataPath": "/safe/highlights",
            "highlightIntentDatabasePath": "/safe/highlights/intents.db",
            "highlightDeliveryPollSeconds": 12,
            "highlightDeliveryInitialDelaySeconds": 4,
            "highlightDeliveryTimeoutSeconds": 150,
            "highlightUnknownRetentionDays": 21,
            "highlightInlineVideoMaxBytes": 8 * 1024 * 1024,
        }
    )
    assert configured.highlight_enabled is True
    assert configured.highlight_data_path == "/safe/highlights"
    assert configured.highlight_intent_database_path == "/safe/highlights/intents.db"
    assert configured.highlight_delivery_poll_seconds == 12
    assert configured.highlight_delivery_initial_delay_seconds == 4
    assert configured.highlight_delivery_timeout_seconds == 150
    assert configured.highlight_unknown_retention_days == 21
    assert configured.highlight_inline_video_max_bytes == 8 * 1024 * 1024


async def test_highlight_delivery_uses_dedicated_onebot_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[float] = []

    async def fake_call_action(
        _self: PWCS2Channel,
        _action: str,
        _params: dict[str, object],
        **kwargs: object,
    ) -> dict[str, object]:
        observed.append(float(kwargs.get("timeout", 20.0)))
        return {"status": "ok", "retcode": 0}

    monkeypatch.setattr(PWCS2Channel, "_call_action", fake_call_action)
    channel = PWCS2Channel(
        {
            "enabled": True,
            "allowFrom": ["*"],
            "pwToken": "pw-secret",
            "databasePath": str(tmp_path / "bindings.db"),
            "allowedGroups": ["987654"],
            "highlightEnabled": True,
            "highlightDataPath": str(tmp_path / "highlights"),
            "highlightIntentDatabasePath": str(tmp_path / "highlights" / "intents.db"),
            "highlightDeliveryTimeoutSeconds": 150,
            "statsImageEnabled": False,
        },
        MessageBus(),
    )
    try:
        assert channel.highlight_delivery is not None
        await channel.highlight_delivery.call_action("send_group_msg", {})
    finally:
        await channel.stop()

    assert observed == [150]
