"""Claude Desktop: best-effort activity tile.

Conversation content is cloud-only (leveldb holds drafts/UI state), so all we
can honestly report is app-running + recent disk activity.
"""
from __future__ import annotations

import subprocess
import time
from pathlib import Path

from ..model import Session, SessionState

APP_SUPPORT = Path.home() / "Library" / "Application Support" / "Claude"
ACTIVE_S = 30

_ACTIVITY_GLOBS = ["*.log", "*-wal", "IndexedDB/*/*.log", "Local Storage/leveldb/*.log"]


def _app_running() -> bool:
    try:
        return subprocess.run(["pgrep", "-xq", "Claude"], timeout=5).returncode == 0
    except (subprocess.SubprocessError, OSError):
        return False


def _latest_activity() -> float:
    latest = 0.0
    for pattern in _ACTIVITY_GLOBS:
        for p in APP_SUPPORT.glob(pattern):
            try:
                latest = max(latest, p.stat().st_mtime)
            except OSError:
                continue
    return latest


def scan() -> Session | None:
    if not APP_SUPPORT.is_dir() or not _app_running():
        return None
    activity = _latest_activity()
    active = time.time() - activity < ACTIVE_S
    return Session(
        key="claude-desktop",
        source="claude-desktop",
        session_id="claude-desktop",
        cwd="",
        name="Claude.app",
        state=SessionState.WORKING if active else SessionState.IDLE,
        state_since=activity or time.time(),
        state_origin="poll",
        last_activity=activity or time.time(),
    )
