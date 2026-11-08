from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).parents[1]
WORKER_ROOT = ROOT / "deploy" / "windows-highlight-worker"


def _read(name: str) -> str:
    return (WORKER_ROOT / name).read_text(encoding="utf-8")


def test_setup_creates_a_pinned_private_virtual_environment_explicitly() -> None:
    setup = _read("setup-worker.ps1")
    assert '[string]$PythonPath = ""' in setup
    assert "[switch]$InstallDependencies" in setup
    assert '$VenvRoot = Join-Path $Root ".venv"' in setup
    assert '"-m", "venv", $VenvRoot' in setup
    assert "if ($InstallDependencies)" in setup
    assert "$VenvPython -m pip install" in setup
    assert "--only-binary=:all:" in setup
    assert "--requirement $Requirements" in setup
    assert setup.index("if ($InstallDependencies)") < setup.index(
        "$VenvPython -m pip install"
    )
    assert not re.search(r"Remove-Item[^\r\n]*\$Venv", setup, re.IGNORECASE)
    assert "demoparser2 0.41.4" not in setup  # Version comes from package metadata.
    assert "value == '0.41.4'" in setup
    assert 'foreach ($Selector in @("-3.12", "-3.13", "-3.11", "-3.10"))' in setup


def test_runner_never_falls_back_to_global_python() -> None:
    runner = _read("run-worker.ps1")
    assert '$VenvPython = Join-Path $Root ".venv\\Scripts\\python.exe"' in runner
    assert runner.count("& $VenvPython @Arguments") == 2
    assert not re.search(r"(?m)^\s*&\s*(?:py|python)(?:\.exe)?\b", runner)
    assert "Get-Command py" not in runner
    assert "Get-Command python" not in runner
    assert " pip " not in runner
    assert "setup-worker.ps1 -InstallDependencies" in runner
    assert "value == '0.41.4'" in runner


def test_secret_setup_avoids_network_credential_plaintext_and_clears_buffers() -> None:
    setup = _read("setup-secret.ps1")
    assert "GetNetworkCredential" not in setup
    assert "PtrToStringBSTR" not in setup
    assert "SecureStringToBSTR" in setup
    assert "Marshal]::Copy($Bstr, $Chars" in setup
    assert "[Text.Encoding]::UTF8.GetBytes($Chars)" in setup
    assert "[Array]::Clear($Plain" in setup
    assert "[Array]::Clear($Chars" in setup
    assert "ZeroFreeBSTR($Bstr)" in setup


def test_worker_scripts_avoid_powershell_7_only_control_operators() -> None:
    scripts = _read("setup-worker.ps1") + "\n" + _read("run-worker.ps1")
    assert "&&" not in scripts
    assert "||" not in scripts
    assert "??" not in scripts
    assert "ForEach-Object -Parallel" not in scripts
    assert "Set-StrictMode -Version 2.0" in scripts


def test_worker_local_environment_is_ignored_and_documented() -> None:
    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    readme = _read("README.md")
    assert "deploy/windows-highlight-worker/.venv/" in gitignore
    assert ".venv\\Scripts\\python.exe" in readme
    assert "-PythonPath" in readme
    assert "-InstallDependencies" in readme
    assert "Python 3.14 is" in readme


def test_recording_switch_is_default_off_and_console_free() -> None:
    starter = _read("start-worker.ps1")
    switch = _read("recording-switch.pyw")
    launcher = _read("open-recording-switch.vbs")
    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    readme = _read("README.md")

    marker_check = 'Test-Path -LiteralPath $EnableMarker -PathType Leaf'
    assert '$EnableMarker = Join-Path $Root ".recording-enabled"' in starter
    assert '$SwitchActive = $env:NIUNIU_HIGHLIGHT_SWITCH_ACTIVE' in starter
    assert '$SwitchActive -ne "1"' in starter
    assert marker_check in starter
    assert starter.index(marker_check) < starter.index("$Ssh = Join-Path")
    assert "CREATE_NO_WINDOW" in switch
    assert "shell=False" in switch
    assert 'ENABLE_MARKER = ROOT / ".recording-enabled"' in switch
    assert "ENABLE_MARKER.unlink(missing_ok=True)" in switch
    assert 'environment["NIUNIU_HIGHLIGHT_SWITCH_ACTIVE"] = "1"' in switch
    assert "self._show_failed_and_disabled()" in switch
    assert switch.index("self.enabled.clear()") < switch.index(
        "self._show_failed_and_disabled()"
    )
    assert '"-NonInteractive"' in switch
    assert ".venv\\Scripts\\pythonw.exe" in launcher
    assert "shell.Run" in launcher and ", 1, False" in launcher
    assert "pythonw.exe has no console" in launcher
    assert "deploy/windows-highlight-worker/.recording-enabled" in gitignore
    assert "Do not create a Scheduled Task" in readme


def test_worker_uses_exact_cs2_process_and_foreground_thread_attachment() -> None:
    worker = _read("worker.py")

    assert 'Path(image.value).name.casefold() == "cs2.exe"' in worker
    assert "QueryFullProcessImageNameW" in worker
    assert "AttachThreadInput" in worker
    assert "BringWindowToTop" in worker
    assert "keybd_event" in worker
    assert "_focus_cs2_window(" in worker
