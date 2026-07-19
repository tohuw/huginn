"""ChatGPT Desktop: process presence plus best-effort local activity.

Conversation text is not read from Electron storage. Local Codex work is
already discovered authoritatively through ``~/.codex/state_5.sqlite``; this
source adds an honest app-level tile for desktop presence/activity.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

from ..model import Session, SessionState
from ..platform import platform as _platform

ACTIVE_S = 30
_ACTIVITY_GLOBS = (
    "*.log", "*-wal", "IndexedDB/*/*.log", "Local Storage/leveldb/*.log",
    "Session Storage/*.log", "Network/*",
)


def _support_dirs() -> list[Path]:
    if sys.platform == "win32":
        roaming = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
        local = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        return [roaming / "Codex", roaming / "ChatGPT", local / "Codex", local / "ChatGPT"]
    base = Path.home() / "Library" / "Application Support"
    return [base / "Codex", base / "com.openai.codex", base / "OpenAI"]


def _app_pids() -> list[int]:
    # Current desktop builds use ChatGPT as the main process; retain Codex as
    # a compatibility name for earlier/Windows builds.
    return sorted(set(_platform.find_processes("ChatGPT") + _platform.find_processes("Codex")))


def _latest_activity() -> float:
    latest = 0.0
    for directory in _support_dirs():
        if not directory.is_dir():
            continue
        for pattern in _ACTIVITY_GLOBS:
            for path in directory.glob(pattern):
                try:
                    latest = max(latest, path.stat().st_mtime)
                except OSError:
                    continue
    return latest


def scan() -> Session | None:
    pids = _app_pids()
    if not pids:
        return None
    activity = _latest_activity()
    now = time.time()
    active = bool(activity and now - activity < ACTIVE_S)
    return Session(
        key="chatgpt-desktop", source="chatgpt-desktop",
        session_id="chatgpt-desktop", cwd="", name="ChatGPT",
        pid=pids[0], entrypoint="desktop",
        state=SessionState.ACTIVE if active else SessionState.IDLE,
        state_since=activity or now, state_origin="poll",
        last_activity=activity or now,
    )
