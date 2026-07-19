"""Native Windows process inspection and best-effort focus routing."""
from __future__ import annotations

import ctypes
import datetime as dt
import json
import os
import shutil
import subprocess
from ctypes import wintypes

from .base import FocusResult, Platform


CREATE_NO_WINDOW = 0x08000000


def _powershell(script: str, timeout: float = 5) -> str:
    executable = shutil.which("pwsh") or shutil.which("powershell") or "powershell.exe"
    try:
        return subprocess.run(
            [executable, "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, timeout=timeout,
            creationflags=CREATE_NO_WINDOW if os.name == "nt" else 0,
        ).stdout.strip()
    except (subprocess.SubprocessError, OSError):
        return ""


def _process_json(where: str) -> list[dict]:
    raw = _powershell(
        f"Get-CimInstance Win32_Process -Filter \"{where}\" | "
        "Select-Object ProcessId,ParentProcessId,Name,CreationDate,CommandLine | ConvertTo-Json -Compress"
    )
    if not raw:
        return []
    try:
        value = json.loads(raw)
        return value if isinstance(value, list) else [value]
    except json.JSONDecodeError:
        return []


class WindowsPlatform(Platform):
    def pid_alive(self, pid: int) -> bool:
        if os.name != "nt":
            return bool(_process_json(f"ProcessId={int(pid)}"))
        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))
        if not handle:
            return False
        ctypes.windll.kernel32.CloseHandle(handle)
        return True

    def process_start_time(self, pid: int) -> float | None:
        if os.name == "nt":
            handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))
            if not handle:
                return None
            created = wintypes.FILETIME()
            exited = wintypes.FILETIME()
            kernel = wintypes.FILETIME()
            user = wintypes.FILETIME()
            try:
                if ctypes.windll.kernel32.GetProcessTimes(
                    handle, ctypes.byref(created), ctypes.byref(exited),
                    ctypes.byref(kernel), ctypes.byref(user),
                ):
                    ticks = (created.dwHighDateTime << 32) | created.dwLowDateTime
                    return ticks / 10_000_000 - 11_644_473_600
            finally:
                ctypes.windll.kernel32.CloseHandle(handle)
            return None
        rows = _process_json(f"ProcessId={int(pid)}")
        if not rows or not rows[0].get("CreationDate"):
            return None
        try:
            return dt.datetime.fromisoformat(rows[0]["CreationDate"]).timestamp()
        except (TypeError, ValueError):
            return None

    def children(self, pid: int) -> list[int]:
        return [int(row["ProcessId"]) for row in _process_json(f"ParentProcessId={int(pid)}")]

    def parent(self, pid: int) -> int | None:
        rows = _process_json(f"ProcessId={int(pid)}")
        value = int(rows[0].get("ParentProcessId") or 0) if rows else 0
        return value or None

    def process_cwd(self, pid: int) -> str | None:
        # Win32_Process does not expose the PEB current directory. Command-line
        # cwd flags are useful for Codex/VS Code, but callers must tolerate None.
        rows = _process_json(f"ProcessId={int(pid)}")
        command = str(rows[0].get("CommandLine") or "") if rows else ""
        for flag in ("--cwd", "-C"):
            marker = f"{flag} "
            if marker in command:
                value = command.split(marker, 1)[1].strip()
                return value[1:].split('"', 1)[0] if value.startswith('"') else value.split()[0]
        return None

    def process_name(self, pid: int) -> str | None:
        rows = _process_json(f"ProcessId={int(pid)}")
        return str(rows[0].get("Name") or "") or None

    def process_tty(self, pid: int) -> str | None:
        return None

    def find_processes(self, executable: str) -> list[int]:
        name = executable if executable.lower().endswith(".exe") else f"{executable}.exe"
        escaped = name.replace("'", "''")
        return [int(row["ProcessId"]) for row in _process_json(f"Name='{escaped}'")]

    @staticmethod
    def _window_for_processes(pids: set[int]) -> int | None:
        if os.name != "nt":
            return None
        user32 = ctypes.windll.user32
        found: list[int] = []
        callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        @callback_type
        def callback(hwnd, _lparam):
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value in pids and user32.IsWindowVisible(hwnd):
                found.append(hwnd)
                return False
            return True

        user32.EnumWindows(callback, 0)
        return found[0] if found else None

    @staticmethod
    def _raise_window(hwnd: int) -> bool:
        if os.name != "nt":
            return False
        user32 = ctypes.windll.user32
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        return bool(user32.SetForegroundWindow(hwnd))

    def focus_terminal(self, pid: int | None, tty: str | None = None) -> FocusResult:
        candidates: set[int] = set()
        current = pid
        for _ in range(12):
            if not current or current in candidates:
                break
            candidates.add(current)
            current = self.parent(current)
        candidates.update(self.find_processes("WindowsTerminal"))
        hwnd = self._window_for_processes(candidates)
        ok = bool(hwnd and self._raise_window(hwnd))
        detail = "Windows Terminal focused; exact tab unavailable" if ok else "Windows Terminal window not found"
        return FocusResult(ok, "Windows Terminal" if ok else None, detail)

    def focus_vscode(self, cwd: str) -> FocusResult:
        executable = shutil.which("code") or shutil.which("code.cmd")
        if not executable:
            return FocusResult(False, detail="VS Code command not found")
        try:
            subprocess.Popen(
                [executable, "--reuse-window", cwd], stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            return FocusResult(True, "VS Code")
        except OSError:
            return FocusResult(False, detail="VS Code could not be opened")

    def activate_app(self, name: str) -> FocusResult:
        aliases = {
            "ChatGPT": ("ChatGPT", "Codex"),
            "Codex": ("Codex", "ChatGPT"),
            "Claude": ("Claude",),
        }
        pids = {pid for candidate in aliases.get(name, (name,)) for pid in self.find_processes(candidate)}
        hwnd = self._window_for_processes(pids)
        ok = bool(hwnd and self._raise_window(hwnd))
        return FocusResult(ok, name if ok else None, None if ok else f"{name} window not found")
