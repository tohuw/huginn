"""Jump-to-session: pid → tty → iTerm2 tab via AppleScript, with fallbacks.

The claude process itself may not own the tty (it wraps a caffeinate child, and
sometimes the tty is only on the parent shell) — walk the process tree both ways.
Degradation chain: iTerm2 by tty → VS Code by cwd → app activate → error.
"""
from __future__ import annotations

import subprocess
from typing import Any

from .model import Session

# Two passes: normal windows first, then `current window` — iTerm2 hotkey
# (dropdown) windows are excluded from `every window` (index -1) and are only
# reachable via `current window` + `reveal hotkey window`. Observed on 3.6.10.
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


def _run(cmd: list[str], timeout: float = 5) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout.strip()
    except (subprocess.SubprocessError, OSError):
        return ""


def _tty_of(pid: int) -> str | None:
    out = _run(["ps", "-o", "tty=", "-p", str(pid)])
    return out if out and out != "??" else None


def _children(pid: int) -> list[int]:
    out = _run(["pgrep", "-P", str(pid)])
    return [int(x) for x in out.split()] if out else []


def _parent(pid: int) -> int | None:
    out = _run(["ps", "-o", "ppid=", "-p", str(pid)])
    try:
        p = int(out)
        return p if p > 1 else None
    except ValueError:
        return None


def find_tty(pid: int) -> str | None:
    """tty of the process, its descendants (2 levels), or its parent shell."""
    tty = _tty_of(pid)
    if tty:
        return tty
    for child in _children(pid):
        tty = _tty_of(child)
        if tty:
            return tty
        for grand in _children(child):
            tty = _tty_of(grand)
            if tty:
                return tty
    parent = _parent(pid)
    if parent:
        return _tty_of(parent)
    return None


def _focus_iterm_tty(tty: str) -> bool:
    dev = tty if tty.startswith("/dev/") else f"/dev/{tty}"
    out = _run(["osascript", "-e", _OSA_FOCUS_TTY, dev], timeout=10)
    return out == "ok"


def _open_app(name: str) -> bool:
    return subprocess.run(["open", "-a", name], capture_output=True).returncode == 0


def focus_session(s: Session) -> dict[str, Any]:
    if s.source == "codex":
        ok = _open_app("ChatGPT")
        return {"ok": ok, "target": "ChatGPT"}
    if s.source == "claude-desktop":
        ok = _open_app("Claude")
        return {"ok": ok, "target": "Claude"}

    # Claude Code: prefer the exact iTerm2 tab
    if s.entrypoint == "cli" and s.pid:
        tty = s.tty or find_tty(s.pid)
        if tty:
            s.tty = tty
            if _focus_iterm_tty(tty):
                return {"ok": True, "target": "iTerm2", "tty": tty}
    # VS Code-attached (or tty not found): open the workspace
    if s.cwd:
        if subprocess.run(["open", "-a", "Visual Studio Code", s.cwd],
                          capture_output=True).returncode == 0:
            return {"ok": True, "target": "VS Code"}
    return {"ok": False, "error": "no focus target found"}
