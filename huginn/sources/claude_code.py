"""Claude Code source: ~/.claude/sessions/<PID>.json status files + liveness.

The status file is authoritative for "a session exists and is busy/idle".
Finer states (waiting for input/permission, done, error) come from hooks and
transcript inference layered on top by the reducer.
"""
from __future__ import annotations

import datetime
import json
from pathlib import Path

from ..model import Session, SessionState
from ..platform import platform as _platform

CLAUDE_DIR = Path.home() / ".claude"
SESSIONS_DIR = CLAUDE_DIR / "sessions"
PROJECTS_DIR = CLAUDE_DIR / "projects"

# status field in the session file -> coarse state
_STATUS_MAP = {
    "busy": SessionState.WORKING,
    "shell": SessionState.WORKING,
    "idle": SessionState.IDLE,
}


def pid_alive(pid: int) -> bool:
    return _platform.pid_alive(pid)


def child_shell_count(pid: int) -> int:
    """Count direct shell children launched by an interactive Claude process.

    Claude's Bash tool and background tasks are direct zsh/bash/sh children.
    Counting only direct children avoids including the commands those shells
    launch (sleep, gh, python, and so on).
    """
    shells = {"sh", "bash", "zsh", "dash", "ksh", "fish", "cmd.exe", "powershell.exe", "pwsh.exe"}
    return sum(1 for child in _platform.children(pid)
               if (_platform.process_name(child) or "").lower() in shells)


def _ps_lstart(pid: int) -> datetime.datetime | None:
    started = _platform.process_start_time(pid)
    return datetime.datetime.fromtimestamp(started) if started is not None else None


def pid_matches_start(pid: int, proc_start: str | None) -> bool:
    """Guard against PID reuse: the recorded procStart must match ps within 5s."""
    if not proc_start:
        return True  # nothing to check against
    recorded = None
    try:
        recorded = datetime.datetime.strptime(proc_start.strip(), "%a %b %d %H:%M:%S %Y")
    except ValueError:
        return True  # unknown format; don't false-negative
    actual = _ps_lstart(pid)
    if actual is None:
        return True
    # procStart in the file is UTC; ps lstart is local time. Accept any delta
    # that is a whole timezone offset (15-min granularity) plus <=5s of skew.
    delta = abs((actual - recorded).total_seconds())
    return delta % 900 <= 5 or delta % 900 >= 895


def find_transcript(session_id: str) -> str | None:
    matches = list(PROJECTS_DIR.glob(f"*/{session_id}.jsonl"))
    return str(matches[0]) if matches else None


def parse_session_file(path: Path) -> Session | None:
    """Parse one ~/.claude/sessions/<PID>.json tolerantly. None if unusable/dead."""
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    pid = raw.get("pid")
    if pid is None:
        try:
            pid = int(path.stem)
        except ValueError:
            return None
    session_id = raw.get("sessionId", "")
    if not session_id:
        return None
    # headless `claude -p` runs (incl. huginn's own blurb/chat calls) register
    # here too with kind "interactive" but entrypoint "sdk-cli" — only real
    # interactive sessions are worth monitoring
    if raw.get("kind") not in (None, "interactive") or raw.get("entrypoint") == "sdk-cli":
        return None

    status = raw.get("status")  # absent pre-2.1.211
    state = _STATUS_MAP.get(status, SessionState.IDLE)
    updated = raw.get("statusUpdatedAt") or raw.get("updatedAt") or raw.get("startedAt") or 0
    ts = updated / 1000.0 if updated else path.stat().st_mtime

    return Session(
        key=f"claude:{pid}",
        source="claude",
        session_id=session_id,
        cwd=raw.get("cwd", ""),
        name=raw.get("name") or Path(raw.get("cwd", "?")).name,
        pid=pid,
        entrypoint=raw.get("entrypoint"),
        state=state,
        state_since=ts,
        state_origin="statusfile" if status else "init",
        transcript_path=find_transcript(session_id),
        last_activity=ts,
        version=raw.get("version"),
        shells=child_shell_count(pid),
    )


def scan(include_dead: bool = False) -> list[Session]:
    """One-shot scan of all Claude Code session status files."""
    sessions: list[Session] = []
    if not SESSIONS_DIR.is_dir():
        return sessions
    for path in sorted(SESSIONS_DIR.glob("*.json")):
        try:
            raw = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        sess = parse_session_file(path)
        if sess is None:
            continue
        alive = pid_alive(sess.pid) and pid_matches_start(sess.pid, raw.get("procStart"))
        if not alive:
            if include_dead:
                sess.state = SessionState.ENDED
                sess.state_origin = "timeout"
                sessions.append(sess)
            continue
        sessions.append(sess)
    return sessions
