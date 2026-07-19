"""Claude Desktop: best-effort activity tile.

Conversation content is cloud-only (leveldb holds drafts/UI state), so all we
can honestly report is app-running + recent disk activity.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

from ..model import Session, SessionState
from ..platform import platform as _platform

APP_SUPPORT = (Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / "Claude"
               if sys.platform == "win32"
               else Path.home() / "Library" / "Application Support" / "Claude")
ACTIVE_S = 30

# Chromium touches Local Storage and housekeeping WALs (notably DIPS-wal)
# every few seconds while Claude is merely open.  They cannot distinguish a
# conversation from an idle renderer and made the tile effectively permanent
# WORKING.  IndexedDB is Claude's conversation-side store; root logs are kept
# for desktop builds that emit explicit application activity there.
_ACTIVITY_GLOBS = ["*.log", "IndexedDB/*/*.log"]


def _app_running() -> bool:
    return bool(_platform.find_processes("Claude"))


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
        name="Claude" if sys.platform == "win32" else "Claude.app",
        state=SessionState.ACTIVE if active else SessionState.IDLE,
        state_since=activity or time.time(),
        state_origin="poll",
        last_activity=activity or time.time(),
    )
