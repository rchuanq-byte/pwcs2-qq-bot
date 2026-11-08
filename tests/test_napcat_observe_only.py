from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "napcat_observe_only", Path(__file__).parents[1] / "deploy" / "napcat_observe_only.py"
)
assert SPEC and SPEC.loader
patcher = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(patcher)


@pytest.fixture
def official_archive() -> Path:
    value = os.environ.get("NAPCAT_OFFICIAL_SHELL_ZIP")
    if not value:
        pytest.skip("Set NAPCAT_OFFICIAL_SHELL_ZIP to run pinned-release integration tests")
    path = Path(value)
    assert path.is_file(), "Configured official archive is missing"
    assert patcher.sha256(path.read_bytes()) == patcher.OFFICIAL_ARCHIVE_SHA256
    return path


@pytest.fixture
def official_bundle(official_archive: Path) -> bytes:
    with zipfile.ZipFile(official_archive) as archive:
        return archive.read("napcat.mjs")


def test_bad_archive_digest_fails_without_creating_output(tmp_path: Path) -> None:
    source = tmp_path / "untrusted.zip"
    source.write_bytes(b"unexpected release")
    output = tmp_path / "candidate.zip"
    with pytest.raises(ValueError, match="archive SHA256"):
        patcher.prepare_archive(source, output)
    assert not output.exists()
    assert list(tmp_path.iterdir()) == [source]


def test_bundle_digest_is_required_before_replacement() -> None:
    with pytest.raises(ValueError, match="napcat.mjs SHA256"):
        patcher.patch_bundle(patcher.OFFLINE_ORIGINAL.encode())


@pytest.mark.parametrize("duplicate", [False, True])
def test_anchor_count_is_fail_closed(
    official_bundle: bytes, monkeypatch: pytest.MonkeyPatch, duplicate: bool
) -> None:
    if duplicate:
        source = official_bundle + patcher.OFFLINE_ORIGINAL.encode()
    else:
        source = official_bundle.replace(patcher.OFFLINE_ORIGINAL.encode(), b"", 1)
    # Exercise the independent anchor check even if someone updates the digest
    # during a future release review without updating its replacement anchors.
    monkeypatch.setattr(patcher, "OFFICIAL_BUNDLE_SHA256", patcher.sha256(source))
    with pytest.raises(ValueError, match="offline_recovery anchor count"):
        patcher.patch_bundle(source)


def test_official_archive_preserves_every_unrelated_file(
    official_archive: Path, tmp_path: Path
) -> None:
    output = tmp_path / "prepared.zip"
    result = patcher.prepare_archive(official_archive, output)
    assert result["policy"] == "observe_only"
    with zipfile.ZipFile(official_archive) as before, zipfile.ZipFile(output) as after:
        assert set(after.namelist()) == set(before.namelist()) | {patcher.MANIFEST_NAME}
        for entry in before.infolist():
            assert after.getinfo(entry.filename).external_attr == entry.external_attr
            if entry.filename != "napcat.mjs":
                assert after.read(entry.filename) == before.read(entry.filename)
        patched = after.read("napcat.mjs")
        assert patcher.sha256(patched) == result["patched_bundle_sha256"]
        assert json.loads(after.read(patcher.MANIFEST_NAME))["policy"] == "observe_only"
    original_output = output.read_bytes()
    with pytest.raises(FileExistsError):
        patcher.prepare_archive(official_archive, output)
    assert output.read_bytes() == original_output


def _node() -> str:
    executable = os.environ.get("NAPCAT_TEST_NODE") or shutil.which("node")
    if not executable:
        pytest.skip("Node is required for behavior checks")
    return executable


def _run_node(script: str, payload: dict) -> None:
    result = subprocess.run(
        [_node(), "-e", script],
        input=json.dumps(payload), text=True, encoding="utf-8", capture_output=True, timeout=15,
    )
    assert result.returncode == 0, result.stderr


def test_patched_bundle_has_valid_module_syntax(official_bundle: bytes) -> None:
    result = subprocess.run(
        [_node(), "--input-type=module", "--check"],
        input=patcher.patch_bundle(official_bundle), capture_output=True, timeout=15,
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")


def test_kick_and_silent_offline_never_restart_but_natural_recovery_remains(
    official_bundle: bytes,
) -> None:
    patched = patcher.patch_bundle(official_bundle).decode()
    start = patched.index("function UAe({")
    end = patched.index("\nclass LAe {", start)
    _run_node(r"""
const assert = require('node:assert/strict');
const vm = require('node:vm');
const payload = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
let online = false, restarts = 0, recovered = 0, offline = 0, nextId = 0;
const timers = new Map();
const context = {
  setTimeout(fn, delay) {
    const timer = { id: ++nextId, fn, delay, unref() {} };
    timers.set(timer.id, timer); return timer;
  },
  clearTimeout(timer) { timers.delete(timer.id); },
};
vm.createContext(context);
vm.runInContext(payload.factory + '\nthis.factory = UAe;', context);
const handle = context.factory({
  isOnline: () => online, onOffline: () => offline++, onRecovered: () => recovered++,
  restart: async () => { restarts++; return true; },
});
function advance() {
  const pending = [...timers.values()]; timers.clear();
  for (const timer of pending) timer.fn();
}
handle('kick', false);
handle('kick again', false);
assert.equal(timers.size, 0);
assert.equal(restarts, 0);
handle('silent', true);
assert.equal([...timers.values()][0].delay, 60000);
advance();
assert.equal(restarts, 0); assert.equal(recovered, 0);
assert.equal(timers.size, 1);
// A long network outage must not restart QQ, and recovery after 60s still updates UI.
for (let minute = 0; minute < 10; minute++) advance();
assert.equal(restarts, 0); assert.equal(recovered, 0); assert.equal(timers.size, 1);
online = true; advance();
assert.equal(recovered, 1); assert.equal(timers.size, 0);
advance(); assert.equal(recovered, 1);
handle('silent again', true); online = true; advance();
assert.equal(restarts, 0); assert.equal(recovered, 2);
online = false; handle('silent', true); handle('kick overrides silent', false);
assert.equal(timers.size, 0); advance();
assert.equal(restarts, 0); assert.equal(recovered, 2); assert.equal(offline, 6);
""", {"factory": patched[start:end]})


def test_unexpected_worker_exit_alerts_without_respawn_or_master_exit(
    official_bundle: bytes,
) -> None:
    patched = patcher.patch_bundle(official_bundle).decode()
    start = patched.index('s.on("exit", (a) => {', patched.index('async function Bg('))
    end = patched.index('}), await new Promise', start)
    callback = patched[start + len('s.on("exit", '):end + 1]
    _run_node(r"""
const assert = require('node:assert/strict');
const vm = require('node:vm');
const payload = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
let respawns = 0, exits = 0, intervals = 0, warnings = 0;
const context = {
  xm: false, DL: false, i: 'test',
  en: { logError() {}, logWarn() { warnings++; } },
  process: { exit() { exits++; } }, Bg() { respawns++; },
  setInterval(fn, delay) { intervals++; return { fn, delay }; },
};
vm.createContext(context);
vm.runInContext('this.onExit = ' + payload.callback, context);
context.onExit(1); context.onExit(0);
assert.equal(respawns, 0); assert.equal(exits, 0);
assert.equal(intervals, 1); assert.equal(warnings, 2);
context.xm = true; context.onExit(0); // deliberate/manual restart remains managed upstream
assert.equal(warnings, 2); assert.equal(intervals, 1);
context.xm = false; context.DL = true; context.onExit(0);
assert.equal(warnings, 2); assert.equal(exits, 0);
""", {"callback": callback})


def test_manual_routes_and_offline_ui_state_are_retained(official_bundle: bytes) -> None:
    source = official_bundle.decode()
    patched = patcher.patch_bundle(official_bundle).decode()
    # Route handlers and the shared dispatch callback still have the exact
    # upstream implementation; only the automatic event path is disconnected.
    anchors = (
        ('async function uge(t, e) {', '\nconst nL = xr();'),
        ('  requestRestartProcess: async function() {', '\n  setQQLoginError(t) {'),
        ('  refreshQRCode: async function() {', '\n};\nvar rd ='),
        ('  }), pe.setRestartProcessCall(async () => {', '\nasync function p2e() {'),
    )
    for start, end in anchors:
        original = source[source.index(start):source.index(end, source.index(start))]
        assert original in patched
    assert patcher.PHASE_PATCHED in patched
    assert patcher.PHASE_ORIGINAL not in patched


def test_explicit_webui_restart_still_dispatches(official_bundle: bytes) -> None:
    patched = patcher.patch_bundle(official_bundle).decode()
    start = patched.index('async function uge(t, e) {')
    end = patched.index('\nconst nL = xr();', start)
    _run_node(r"""
const assert = require('node:assert/strict');
const vm = require('node:vm');
const payload = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
let requests = 0;
const context = {
  pe: { async requestRestartProcess() { requests++; return { result: true }; } },
  he(response, body) { return { ok: true, body }; },
  te(response, message) { return { ok: false, message }; },
};
vm.createContext(context);
vm.runInContext(payload.handler + '\nthis.handler = uge;', context);
(async () => {
  const result = await context.handler({}, {});
  assert.equal(requests, 1); assert.equal(result.ok, true);
  context.pe.requestRestartProcess = async () => ({ result: false, message: 'not available' });
  const failure = await context.handler({}, {});
  assert.equal(failure.ok, false); assert.equal(failure.message, 'not available');
})().catch(error => { console.error(error); process.exitCode = 1; });
""", {"handler": patched[start:end]})
