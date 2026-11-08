import asyncio
import json
import time
from pathlib import Path

import pytest

from pwcs2_bot.healthcheck import read_problem, snapshot_problem
from pwcs2_bot.transport_health import TransportHealthMonitor


class Socket:
    def __init__(self) -> None:
        self.closed = 0
        self.aborted = 0
        self.transport = self

    async def close(self, **_kwargs) -> None:
        self.closed += 1

    def abort(self) -> None:
        self.aborted += 1


def status(*, online=True, good=True):
    return {"status": "ok", "retcode": 0, "data": {"online": online, "good": good}}


async def test_online_health_and_offline_without_restart(tmp_path: Path) -> None:
    socket = Socket()
    response = status()

    async def probe():
        return response

    path = tmp_path / "runtime" / "napcat-health.json"
    monitor = TransportHealthMonitor(path=path, connection=lambda: socket, probe=probe)
    assert await monitor.check_once() == "online"
    snapshot = json.loads(path.read_text())
    assert read_problem(path) is None
    last_ok = snapshot["last_ok_at"]
    response = status(online=False, good=False)
    for _ in range(4):
        assert await monitor.check_once() == "login_required"
    assert socket.closed == 0
    assert read_problem(path) == "qq_not_ready"
    assert json.loads(path.read_text())["last_ok_at"] == last_ok
    response = status()
    assert await monitor.check_once() == "online"
    assert read_problem(path) is None
    assert not list(path.parent.glob(".napcat-health-*"))


async def test_transport_error_requires_consecutive_failures_and_hides_secrets(tmp_path):
    socket = Socket()
    failing = True

    async def probe():
        if failing:
            raise RuntimeError("access_token=super-secret https://private.example")
        return status()

    path = tmp_path / "health.json"
    monitor = TransportHealthMonitor(path=path, connection=lambda: socket, probe=probe)
    assert await monitor.check_once() == "unresponsive"
    assert socket.closed == 0
    failing = False
    assert await monitor.check_once() == "online"
    failing = True
    assert await monitor.check_once() == "unresponsive"
    assert socket.closed == 0
    assert await monitor.check_once() == "reconnecting"
    assert socket.closed == 1
    assert "secret" not in path.read_text()
    assert "private.example" not in path.read_text()


async def test_connection_change_does_not_close_new_socket(tmp_path):
    old, new = Socket(), Socket()
    current = old

    async def probe():
        nonlocal current
        current = new
        raise RuntimeError("old connection failed")

    monitor = TransportHealthMonitor(
        path=tmp_path / "health.json", connection=lambda: current, probe=probe,
        failure_threshold=1,
    )
    assert await monitor.check_once() == "reconnecting"
    assert old.closed == new.closed == 0


async def test_disconnected_snapshot_and_new_connection_reset_failures(tmp_path):
    current = Socket()

    async def probe():
        raise TimeoutError

    path = tmp_path / "health.json"
    monitor = TransportHealthMonitor(path=path, connection=lambda: current, probe=probe)
    assert await monitor.check_once() == "unresponsive"
    current = None
    assert await monitor.check_once() == "disconnected"
    assert json.loads(path.read_text())["connected"] is False
    current = Socket()
    assert await monitor.check_once() == "unresponsive"
    assert current.closed == 0


async def test_hung_probe_and_close_are_bounded(tmp_path):
    class HungSocket(Socket):
        async def close(self, **_kwargs):
            self.closed += 1
            await asyncio.Event().wait()

    socket = HungSocket()

    async def probe():
        await asyncio.Event().wait()

    monitor = TransportHealthMonitor(
        path=tmp_path / "health.json", connection=lambda: socket, probe=probe,
        timeout=0.01, failure_threshold=1,
    )
    assert await asyncio.wait_for(monitor.check_once(), timeout=1) == "reconnecting"
    assert socket.closed == socket.aborted == 1


@pytest.mark.parametrize("response", [
    {}, None, {"status": "ok", "retcode": 0, "data": {}},
    status(online="false"), status(good=None), {**status(), "retcode": 1},
])
async def test_invalid_status_is_not_healthy(tmp_path, response):
    async def probe():
        return response

    socket = Socket()
    path = tmp_path / "health.json"
    monitor = TransportHealthMonitor(path=path, connection=lambda: socket, probe=probe)
    assert await monitor.check_once() == "unresponsive"
    assert read_problem(path) == "qq_not_ready"


async def test_monitor_stops_cleanly_and_never_restarts_qq(tmp_path):
    socket = Socket()
    probed = asyncio.Event()

    async def probe():
        probed.set()
        return status()

    path = tmp_path / "health.json"
    monitor = TransportHealthMonitor(
        path=path, connection=lambda: socket, probe=probe, interval=0.01
    )
    task = asyncio.create_task(monitor.run())
    await asyncio.wait_for(probed.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert json.loads(path.read_text())["state"] == "stopped"
    assert socket.closed == 0


async def test_cancellation_is_not_a_failed_health_probe(tmp_path):
    entered = asyncio.Event()
    socket = Socket()

    async def probe():
        entered.set()
        await asyncio.Event().wait()

    monitor = TransportHealthMonitor(
        path=tmp_path / "health.json", connection=lambda: socket, probe=probe,
        failure_threshold=1,
    )
    task = asyncio.create_task(monitor.check_once())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert socket.closed == 0


@pytest.mark.parametrize("checked", [None, True, "123", float("nan"), float("inf"), 0, 106])
def test_healthcheck_rejects_stale_or_invalid_time(checked):
    snapshot = {"version": 1, "state": "online", "checked_at": checked,
                "online": True, "connected": True}
    assert snapshot_problem(snapshot, now=100) == "stale_snapshot"


def test_healthcheck_requires_fresh_connection_and_gateway_schema(tmp_path):
    assert snapshot_problem([], now=100) == "invalid_snapshot"
    data = {"version": 1, "state": "online", "checked_at": 100,
            "online": True, "connected": True}
    assert snapshot_problem(data, now=110) is None
    assert snapshot_problem({**data, "online": "true"}, now=110) == "qq_not_ready"
    path = tmp_path / "health.json"
    assert read_problem(path) == "missing_or_invalid_snapshot"
    path.write_text("x" * 8193)
    assert read_problem(path) == "invalid_snapshot"
    data["checked_at"] = time.time()
    path.write_text(json.dumps(data))
    assert read_problem(path) is None


def test_compose_uses_real_qq_readiness():
    compose = (Path(__file__).parents[1] / "compose.yaml").read_text()
    bot = compose.split("\n  bot:\n", 1)[1].split("\n  highlight-controller:", 1)[0]
    assert 'test: ["CMD", "python", "-m", "pwcs2_bot.healthcheck"]' in bot
    assert "start_period: 60s" in bot


def test_code_update_image_source_is_readable_by_unprivileged_bot():
    dockerfile = Path(__file__).parents[1] / "deploy" / "Dockerfile.code-update"
    assert "COPY --chown=bot:bot src /app/src" in dockerfile.read_text()
