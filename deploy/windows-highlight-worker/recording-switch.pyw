"""Console-free, user-controlled launcher for the Windows highlight worker."""

from __future__ import annotations

import ctypes
import json
import os
from pathlib import Path
import subprocess
import threading
import tkinter as tk
from tkinter import messagebox


ROOT = Path(__file__).resolve().parent
ENABLE_MARKER = ROOT / ".recording-enabled"
CONFIG_PATH = ROOT / "config.json"
START_WORKER = ROOT / "start-worker.ps1"
LOG_PATH = ROOT / "recording-switch.log"
MUTEX_NAME = "Local\\NiuniuCs2HighlightRecordingSwitch"
ERROR_ALREADY_EXISTS = 183
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)


def _powershell_path() -> Path:
    windows_dir = Path(os.environ.get("WINDIR", r"C:\Windows"))
    return windows_dir / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"


def _poll_seconds() -> int:
    try:
        payload = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        value = int(payload.get("poll_seconds", 20))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return 20
    return max(5, min(value, 3600))


def _set_enabled(enabled: bool) -> None:
    if enabled:
        temporary = ENABLE_MARKER.with_suffix(".tmp")
        temporary.write_text("enabled by recording-switch.pyw\n", encoding="utf-8")
        temporary.replace(ENABLE_MARKER)
    else:
        ENABLE_MARKER.unlink(missing_ok=True)


def _acquire_single_instance() -> int | None:
    if os.name != "nt":
        return 1
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_bool
    handle = kernel32.CreateMutexW(None, False, MUTEX_NAME)
    if not handle or kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
        if handle:
            kernel32.CloseHandle(handle)
        return None
    return int(handle)


class RecordingSwitch:
    def __init__(self, root: tk.Tk, mutex_handle: int) -> None:
        self.root = root
        self.mutex_handle = mutex_handle
        self.enabled = threading.Event()
        self.closing = threading.Event()
        self.worker_process: subprocess.Popen[bytes] | None = None

        self.root.title("牛牛精灵 · CS2 高光录制")
        self.root.geometry("430x230")
        self.root.resizable(False, False)
        self.root.protocol("WM_DELETE_WINDOW", self.close)

        frame = tk.Frame(root, padx=28, pady=24)
        frame.pack(fill="both", expand=True)
        tk.Label(frame, text="CS2 高光录制", font=("Microsoft YaHei UI", 18, "bold")).pack()
        self.status = tk.StringVar(value="已关闭 · 不连接服务器")
        tk.Label(
            frame,
            textvariable=self.status,
            font=("Microsoft YaHei UI", 11),
            fg="#666666",
            pady=18,
        ).pack()
        self.button = tk.Button(
            frame,
            text="开启高光录制",
            command=self.toggle,
            font=("Microsoft YaHei UI", 12, "bold"),
            width=20,
            height=2,
            bg="#2f9e44",
            fg="white",
            activebackground="#2b8a3e",
        )
        self.button.pack()
        tk.Label(
            frame,
            text="关闭窗口等同于关闭开关；正在录制的当前视频会安全完成。",
            font=("Microsoft YaHei UI", 9),
            fg="#888888",
            pady=14,
        ).pack()

        # Default to OFF even after an abnormal previous shutdown.
        _set_enabled(False)
        threading.Thread(target=self._worker_loop, daemon=True).start()

    def _update_status(self, text: str) -> None:
        try:
            self.root.after(0, self.status.set, text)
        except (tk.TclError, RuntimeError):
            pass

    def _show_failed_and_disabled(self) -> None:
        def apply() -> None:
            self.status.set("录制失败 · 已自动关闭（请查看日志）")
            self.button.configure(
                text="开启高光录制",
                bg="#2f9e44",
                activebackground="#2b8a3e",
            )

        try:
            self.root.after(0, apply)
        except (tk.TclError, RuntimeError):
            pass

    def toggle(self) -> None:
        if self.enabled.is_set():
            self.disable()
        else:
            self.enable()

    def enable(self) -> None:
        powershell = _powershell_path()
        required = (powershell, START_WORKER, CONFIG_PATH, ROOT / "worker-secret.dpapi")
        missing = [str(path) for path in required if not path.is_file()]
        if missing:
            messagebox.showerror("无法开启", "缺少必要文件：\n" + "\n".join(missing))
            return
        _set_enabled(True)
        self.enabled.set()
        self.status.set("已开启 · 正在检查服务器任务")
        self.button.configure(text="关闭高光录制", bg="#c92a2a", activebackground="#a61e1e")

    def disable(self) -> None:
        self.enabled.clear()
        _set_enabled(False)
        if self.worker_process is not None and self.worker_process.poll() is None:
            text = "已关闭 · 当前视频完成后停止"
        else:
            text = "已关闭 · 不连接服务器"
        self.status.set(text)
        self.button.configure(text="开启高光录制", bg="#2f9e44", activebackground="#2b8a3e")

    def _run_one_cycle(self) -> int:
        powershell = _powershell_path()
        environment = os.environ.copy()
        environment["NIUNIU_HIGHLIGHT_SWITCH_ACTIVE"] = "1"
        arguments = [
            str(powershell),
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(START_WORKER),
        ]
        with LOG_PATH.open("ab", buffering=0) as log:
            self.worker_process = subprocess.Popen(
                arguments,
                cwd=ROOT,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                env=environment,
                shell=False,
                creationflags=CREATE_NO_WINDOW,
            )
            return self.worker_process.wait()

    def _worker_loop(self) -> None:
        delay = _poll_seconds()
        while not self.closing.is_set():
            if not self.enabled.wait(timeout=0.5):
                continue
            if self.closing.is_set() or not ENABLE_MARKER.is_file():
                continue
            self._update_status("已开启 · 正在检查服务器任务")
            try:
                exit_code = self._run_one_cycle()
            except OSError as exc:
                self._update_status(f"启动失败：{exc}")
                exit_code = 1
            finally:
                self.worker_process = None
            if not self.enabled.is_set():
                continue
            if exit_code == 0:
                self._update_status(f"已开启 · 等待任务（{delay} 秒后再检查）")
            else:
                # Never burn through every server retry while Windows refuses
                # foreground control or CS2 needs user attention.  One failed
                # cycle closes the local gate; the user can inspect the log and
                # explicitly reopen it after the desktop is ready.
                self.enabled.clear()
                _set_enabled(False)
                self._show_failed_and_disabled()
            remaining = delay
            while remaining > 0 and self.enabled.is_set() and not self.closing.is_set():
                step = min(0.5, remaining)
                self.closing.wait(timeout=step)
                remaining -= step

    def close(self) -> None:
        self.disable()
        self.closing.set()
        if os.name == "nt" and self.mutex_handle:
            ctypes.windll.kernel32.CloseHandle(self.mutex_handle)
            self.mutex_handle = 0
        self.root.destroy()


def main() -> None:
    mutex_handle = _acquire_single_instance()
    if mutex_handle is None:
        root = tk.Tk()
        root.withdraw()
        messagebox.showinfo("高光录制开关", "开关窗口已经在运行。")
        root.destroy()
        return
    root = tk.Tk()
    RecordingSwitch(root, mutex_handle)
    root.mainloop()


if __name__ == "__main__":
    main()
