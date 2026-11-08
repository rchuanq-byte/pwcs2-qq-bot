import asyncio
from pathlib import Path

from pwcs2_bot.database import BindingRepository
from pwcs2_bot.stream_live import LiveSnapshot, StreamerTarget, StreamLiveError
from pwcs2_bot.stream_live_monitor import StreamLiveMonitor


class FakeClient:
    def __init__(self, snapshots: list[LiveSnapshot | Exception]) -> None:
        self.snapshots = snapshots
        self.closed = False

    async def fetch(self, _target: StreamerTarget) -> LiveSnapshot:
        result = self.snapshots.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    async def close(self) -> None:
        self.closed = True


def _snapshot(
    live: bool,
    session: str = "",
    *,
    is_replay: bool = False,
) -> LiveSnapshot:
    return LiveSnapshot(
        platform="douyu",
        room_id="6979222",
        streamer_name="玩机器",
        is_live=live,
        session_key=session,
        title="小众宝藏结晶直播间",
        category="CS2",
        cover_url="",
        live_url="https://www.douyu.com/6657",
        started_at=session,
        is_replay=is_replay,
    )


async def test_monitor_confirms_edge_and_sends_once(tmp_path: Path) -> None:
    repository = BindingRepository(tmp_path / "bot.db")
    client = FakeClient(
        [_snapshot(False), _snapshot(True, "session-a"), _snapshot(True, "session-a")]
    )
    sent: list[tuple[str, str]] = []

    async def send(group_id: str, content: str) -> None:
        sent.append((group_id, content))

    monitor = StreamLiveMonitor(
        repository,
        client,  # type: ignore[arg-type]
        send,
        targets=[StreamerTarget(platform="douyu", name="玩机器", room_id="6979222")],
        allowed_groups=["123"],
        confirmation_polls=2,
    )
    await monitor.poll_once()
    await monitor.poll_once()
    assert sent == []
    await monitor.poll_once()
    assert len(sent) == 1
    assert sent[0][0] == "123"
    assert "玩机器" in sent[0][1]


async def test_monitor_provider_failure_does_not_change_offline_state(
    tmp_path: Path,
) -> None:
    repository = BindingRepository(tmp_path / "bot.db")
    client = FakeClient([_snapshot(True, "session-a"), StreamLiveError("bad payload")])

    async def send(_group_id: str, _content: str) -> None:
        raise AssertionError("baseline and unknown states must not send")

    monitor = StreamLiveMonitor(
        repository,
        client,  # type: ignore[arg-type]
        send,
        targets=[StreamerTarget(platform="douyu", name="玩机器", room_id="6979222")],
        allowed_groups=["123"],
        confirmation_polls=1,
    )
    await monitor.poll_once()
    await monitor.poll_once()
    state = repository.stream_live_state("douyu", "6979222")
    assert state is not None
    assert state["confirmed_live"] is True


async def test_monitor_suppresses_replay_then_notifies_real_live(tmp_path: Path) -> None:
    repository = BindingRepository(tmp_path / "bot.db")
    client = FakeClient(
        [
            _snapshot(False),
            _snapshot(True, "replay-session", is_replay=True),
            _snapshot(True, "real-session"),
        ]
    )
    sent: list[tuple[str, str]] = []

    async def send(group_id: str, content: str) -> None:
        sent.append((group_id, content))

    monitor = StreamLiveMonitor(
        repository,
        client,  # type: ignore[arg-type]
        send,
        targets=[StreamerTarget(platform="douyu", name="玩机器", room_id="6979222")],
        allowed_groups=["123"],
        confirmation_polls=1,
    )

    await monitor.poll_once()
    replay_result = await monitor.poll_once()
    assert replay_result[0].is_replay is True
    assert replay_result[0].is_live is False
    assert sent == []

    await monitor.poll_once()
    assert len(sent) == 1
    assert "玩机器" in sent[0][1]


async def test_monitor_with_no_allowed_groups_is_disabled(tmp_path: Path) -> None:
    monitor = StreamLiveMonitor(
        BindingRepository(tmp_path / "bot.db"),
        FakeClient([]),  # type: ignore[arg-type]
        lambda _group_id, _content: None,  # type: ignore[arg-type]
        targets=[StreamerTarget(platform="douyu", name="玩机器", room_id="6979222")],
        allowed_groups=[],
    )
    assert monitor.enabled is False


async def test_monitor_retries_explicit_send_failure_then_marks_sent(
    tmp_path: Path,
) -> None:
    repository = BindingRepository(tmp_path / "bot.db")
    client = FakeClient([_snapshot(False), _snapshot(True, "session-a")])
    attempts = 0

    async def send(_group_id: str, _content: str) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("OneBot rejected send")

    monitor = StreamLiveMonitor(
        repository,
        client,  # type: ignore[arg-type]
        send,
        targets=[StreamerTarget(platform="douyu", name="Machine", room_id="6979222")],
        allowed_groups=[" 123 ", "", "123"],
        confirmation_polls=1,
        delivery_base_delay_seconds=0,
    )
    assert monitor.allowed_groups == ("123",)
    await monitor.poll_once()
    await monitor.poll_once()
    assert attempts == 1

    await monitor._deliver_pending()  # noqa: SLF001 - delivery retry behavior
    assert attempts == 2
    assert repository.claim_stream_live_notifications() == ()


async def test_monitor_cancellation_releases_claim(tmp_path: Path) -> None:
    repository = BindingRepository(tmp_path / "bot.db")
    client = FakeClient([_snapshot(False), _snapshot(True, "session-a")])

    async def cancel_send(_group_id: str, _content: str) -> None:
        raise asyncio.CancelledError

    monitor = StreamLiveMonitor(
        repository,
        client,  # type: ignore[arg-type]
        cancel_send,
        targets=[StreamerTarget(platform="douyu", name="Machine", room_id="6979222")],
        allowed_groups=["123"],
        confirmation_polls=1,
        delivery_base_delay_seconds=0,
    )
    await monitor.poll_once()
    try:
        await monitor.poll_once()
    except asyncio.CancelledError:
        pass
    else:
        raise AssertionError("cancellation must propagate")

    claimed = repository.claim_stream_live_notifications()
    assert len(claimed) == 1
    assert claimed[0].attempt_count == 2
