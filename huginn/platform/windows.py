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

GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x00000080
GA_ROOT = 2

SYNCHRONIZE = 0x0010_0000
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
WAIT_TIMEOUT = 0x0000_0102
ERROR_ACCESS_DENIED = 5


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


class _PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", ctypes.c_wchar * 260),
    ]


def _toolhelp_parents() -> dict[int, int] | None:
    """Every pid's parent, from one process snapshot. None if unavailable.

    Focus walks up to twelve levels of ancestry, and each level used to be a
    separate ``Get-CimInstance`` -- one PowerShell process per hop, measured at
    0.42s each, so a jump spent about two seconds finding a window before it
    could raise it. The whole table comes back from a single snapshot here, so
    the walk costs one call regardless of depth. ``_process_json`` remains the
    fallback and the only path off Windows.
    """
    if os.name != "nt":
        return None
    kernel32 = ctypes.windll.kernel32
    snapshot = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)  # TH32CS_SNAPPROCESS
    if snapshot == -1 or not snapshot:
        return None
    try:
        entry = _PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(_PROCESSENTRY32W)
        if not kernel32.Process32FirstW(snapshot, ctypes.byref(entry)):
            return None
        parents: dict[int, int] = {}
        while True:
            parents[entry.th32ProcessID] = entry.th32ParentProcessID
            if not kernel32.Process32NextW(snapshot, ctypes.byref(entry)):
                break
        return parents
    finally:
        kernel32.CloseHandle(snapshot)


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
        """Is this process running -- not merely still addressable?

        Opening a handle is not the question, and answering it that way is why
        the roster filled up with corpses. A Windows process object outlives the
        process itself for as long as anyone holds a handle to it, and Huginn is
        one of those holders, so ``OpenProcess`` kept succeeding for sessions
        that had exited minutes earlier. Every dead session stayed in the roster
        in whatever state it died in, and ``triage`` reported them as live
        sessions competing for a worktree.

        A zero-timeout wait is the actual question: a process object is
        unsignalled while it runs and signalled the instant it exits, so an
        exited-but-still-open handle answers correctly.
        """
        if os.name != "nt":
            return bool(_process_json(f"ProcessId={int(pid)}"))
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.OpenProcess(SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION,
                                      False, int(pid))
        if not handle:
            # Owned by another user: it exists, which is what was asked.
            return ctypes.get_last_error() == ERROR_ACCESS_DENIED
        try:
            return kernel32.WaitForSingleObject(handle, 0) == WAIT_TIMEOUT
        finally:
            kernel32.CloseHandle(handle)

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
        parents = _toolhelp_parents()
        if parents is not None:
            return parents.get(int(pid)) or None
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
    def _is_app_window(hwnd: int) -> bool:
        """Is this a window a user could actually switch to?

        ``IsWindowVisible`` alone is far too generous. Explorer keeps a pile of
        visible bookkeeping windows -- ``ThumbnailDeviceHelperWnd``,
        ``DummyDWMListenerWindow``, ``Progman``, ``Shell_TrayWnd`` -- and taking
        the first visible one meant focus targeted an invisible helper and
        appeared to do nothing at all. Every one of those sets
        ``WS_EX_TOOLWINDOW``, which is precisely the "keep me out of the
        switcher" flag, so honouring it is what separates them from a terminal.
        A real top-level window with a caption is what remains.
        """
        user32 = ctypes.windll.user32
        if not user32.IsWindowVisible(hwnd):
            return False
        if user32.GetAncestor(hwnd, GA_ROOT) != hwnd:
            return False
        if user32.GetWindowLongW(hwnd, GWL_EXSTYLE) & WS_EX_TOOLWINDOW:
            return False
        return user32.GetWindowTextLengthW(hwnd) > 0

    @staticmethod
    def _window_for_processes(pids) -> int | None:
        """First app window owned by these pids, in the order given.

        Order is the whole point when the caller passes a process ancestry:
        launching a terminal from Explorer puts Explorer in that ancestry, and
        Explorer owns perfectly legitimate windows of its own. Nearest ancestor
        first means the terminal hosting the session wins over the file manager
        that happened to start it.
        """
        if os.name != "nt":
            return None
        ordered = list(pids)
        wanted = set(ordered)
        user32 = ctypes.windll.user32
        by_pid: dict[int, int] = {}
        callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        @callback_type
        def callback(hwnd, _lparam):
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            # EnumWindows runs in Z-order, so the first hit per pid is that
            # process's frontmost window.
            if pid.value in wanted and pid.value not in by_pid and WindowsPlatform._is_app_window(hwnd):
                by_pid[pid.value] = hwnd
            return True

        user32.EnumWindows(callback, 0)
        return next((by_pid[pid] for pid in ordered if pid in by_pid), None)

    @staticmethod
    def _raise_window(hwnd: int) -> bool:
        """Bring a window to the foreground, defeating the foreground lock.

        Windows only lets the current foreground process hand focus away, so a
        background daemon's bare ``SetForegroundWindow`` is refused. Attaching
        to the foreground thread's input queue for the duration is the
        documented way around it, and without it a jump from the dashboard
        fails on a machine where nothing is wrong.
        """
        if os.name != "nt":
            return False
        user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
        foreground = user32.GetForegroundWindow()
        if foreground == hwnd:
            return True
        ours = kernel32.GetCurrentThreadId()
        threads = {
            user32.GetWindowThreadProcessId(hwnd, None),
            user32.GetWindowThreadProcessId(foreground, None) if foreground else 0,
        } - {0, ours}
        attached = [tid for tid in threads if user32.AttachThreadInput(ours, tid, True)]
        try:
            # Only un-minimize. SW_RESTORE on a maximized window un-maximizes
            # it, so restoring unconditionally would resize a terminal the user
            # deliberately maximized every time they jumped to it.
            if user32.IsIconic(hwnd):
                user32.ShowWindow(hwnd, 9)  # SW_RESTORE
            user32.BringWindowToTop(hwnd)
            return bool(user32.SetForegroundWindow(hwnd))
        finally:
            for tid in attached:
                user32.AttachThreadInput(ours, tid, False)

    def focus_terminal(self, pid: int | None, tty: str | None = None) -> FocusResult:
        # The session's own ancestry, nearest first, and searched alone. A shell
        # hosted by Windows Terminal reaches WindowsTerminal.exe by walking
        # parents (pwsh -> claude -> pwsh -> WindowsTerminal), so the ancestry
        # names the window actually hosting this session. It keeps walking past
        # the terminal to whatever launched it -- usually Explorer -- which is
        # why order matters as much as membership: Explorer owns real windows
        # too, and an unordered set let a File Explorer window outrank the
        # terminal.
        ancestry: list[int] = []
        current = pid
        for _ in range(12):
            if not current or current in ancestry:
                break
            ancestry.append(current)
            current = self.parent(current)
        hwnd = self._window_for_processes(ancestry)
        if not hwnd:
            # Ancestry can be broken -- a reparented shell, or a session Huginn
            # only knows from a transcript. One terminal window is still a
            # better answer than none, and with a single window it is the right
            # one; the detail says the tab is not exact either way.
            hwnd = self._window_for_processes(set(self.find_processes("WindowsTerminal")))
        if not hwnd:
            return FocusResult(False, None, "Windows Terminal window not found")
        if not self._raise_window(hwnd):
            return FocusResult(False, None, "Windows Terminal could not be brought to the foreground")
        return FocusResult(True, "Windows Terminal", "Windows Terminal focused; exact tab unavailable")

    def send_terminal_text(self, pid: int | None, tty: str | None, text: str) -> FocusResult:
        return FocusResult(
            False,
            detail="safe exact-tab steering is not available on Windows Terminal",
        )

    def interrupt_terminal(self, pid: int | None, tty: str | None) -> FocusResult:
        return FocusResult(
            False,
            detail="safe exact-tab interruption is not available on Windows Terminal",
        )

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
