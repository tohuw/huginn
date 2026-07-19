"""macOS process inspection and focus implementation."""
from __future__ import annotations

import datetime as dt
import ctypes
import os
import subprocess

from .base import FocusResult, Platform


def run(cmd: list[str], timeout: float = 5) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout.strip()
    except (subprocess.SubprocessError, OSError):
        return ""


_OSA_FOCUS_TTY = '''
on run argv
  set targetTty to item 1 of argv
  tell application "iTerm2"
    repeat with w in windows
      repeat with t in tabs of w
        repeat with s in sessions of t
          if tty of s is targetTty then
            select w
            tell w to select t
            try
              tell t to select s
            end try
            activate
            return "ok"
          end if
        end repeat
      end repeat
    end repeat
    try
      tell current window
        repeat with t in tabs
          repeat with s in sessions of t
            if tty of s is targetTty then
              select t
              try
                tell t to select s
              end try
              try
                reveal hotkey window
              end try
              return "ok"
            end if
          end repeat
        end repeat
      end tell
    end try
  end tell
  return "notfound"
end run
'''


class _ProcBSDInfo(ctypes.Structure):
    _fields_ = [
        ("flags", ctypes.c_uint32), ("status", ctypes.c_uint32),
        ("xstatus", ctypes.c_uint32), ("pid", ctypes.c_uint32),
        ("ppid", ctypes.c_uint32), ("uid", ctypes.c_uint32),
        ("gid", ctypes.c_uint32), ("ruid", ctypes.c_uint32),
        ("rgid", ctypes.c_uint32), ("svuid", ctypes.c_uint32),
        ("svgid", ctypes.c_uint32), ("rfu_1", ctypes.c_uint32),
        ("comm", ctypes.c_char * 16), ("name", ctypes.c_char * 32),
        ("nfiles", ctypes.c_uint32), ("pgid", ctypes.c_uint32),
        ("pjobc", ctypes.c_uint32), ("e_tdev", ctypes.c_uint32),
        ("e_tpgid", ctypes.c_uint32), ("nice", ctypes.c_int32),
        ("start_tvsec", ctypes.c_uint64), ("start_tvusec", ctypes.c_uint64),
    ]


def _native_process_start_time(pid: int) -> float | None:
    """Read proc_bsdinfo's integer start timeval through libproc."""
    try:
        libproc = ctypes.CDLL("/usr/lib/libproc.dylib")
        info = _ProcBSDInfo()
        size = libproc.proc_pidinfo(
            int(pid), 3, 0, ctypes.byref(info), ctypes.sizeof(info)  # PROC_PIDTBSDINFO
        )
        if size == ctypes.sizeof(info) and info.start_tvsec:
            return info.start_tvsec + info.start_tvusec / 1_000_000
    except (AttributeError, OSError):
        pass
    return None


class MacOSPlatform(Platform):
    def pid_alive(self, pid: int) -> bool:
        try:
            os.kill(pid, 0)
            return True
        except (OSError, ValueError):
            return False

    def process_start_time(self, pid: int) -> float | None:
        native = _native_process_start_time(pid)
        if native is not None:
            return native
        value = run(["ps", "-o", "lstart=", "-p", str(pid)])
        try:
            parsed = dt.datetime.strptime(value, "%a %b %d %H:%M:%S %Y")
            return parsed.astimezone().timestamp()
        except ValueError:
            return None

    def children(self, pid: int) -> list[int]:
        value = run(["pgrep", "-P", str(pid)])
        try:
            return [int(item) for item in value.split()]
        except ValueError:
            return []

    def parent(self, pid: int) -> int | None:
        try:
            value = int(run(["ps", "-o", "ppid=", "-p", str(pid)]))
            return value if value > 1 else None
        except ValueError:
            return None

    def process_cwd(self, pid: int) -> str | None:
        value = run(["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"])
        return next((line[1:] for line in value.splitlines() if line.startswith("n")), None)

    def process_name(self, pid: int) -> str | None:
        value = run(["ps", "-o", "comm=", "-p", str(pid)])
        return os.path.basename(value) if value else None

    def process_tty(self, pid: int) -> str | None:
        value = run(["ps", "-o", "tty=", "-p", str(pid)])
        return value if value and value != "??" else None

    def find_processes(self, executable: str) -> list[int]:
        value = run(["pgrep", "-x", executable])
        try:
            return [int(item) for item in value.split()]
        except ValueError:
            return []

    def focus_terminal(self, pid: int | None, tty: str | None = None) -> FocusResult:
        if not tty:
            return FocusResult(False, detail="terminal tty not found")
        dev = tty if tty.startswith("/dev/") else f"/dev/{tty}"
        ok = run(["osascript", "-e", _OSA_FOCUS_TTY, dev], timeout=10) == "ok"
        return FocusResult(ok, "iTerm2" if ok else None, None if ok else "iTerm2 tab not found")

    def focus_vscode(self, cwd: str) -> FocusResult:
        ok = subprocess.run(
            ["open", "-a", "Visual Studio Code", cwd], capture_output=True
        ).returncode == 0
        return FocusResult(ok, "VS Code" if ok else None)

    def activate_app(self, name: str) -> FocusResult:
        ok = subprocess.run(["open", "-a", name], capture_output=True).returncode == 0
        return FocusResult(ok, name if ok else None)
