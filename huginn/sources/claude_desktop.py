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

def _windows_app_support() -> Path:
    r"""Where Claude Desktop keeps its data on Windows.

    ``%APPDATA%\Claude`` is right for the plain installer and **wrong for the
    Store build**, which is what ships today: an MSIX package gets a redirected
    profile, so its "roaming appdata" is really
    ``%LOCALAPPDATA%\Packages\<package>\LocalCache\Roaming\Claude``. Nothing is
    created at the unredirected path at all, so ``scan()`` saw no directory and
    returned None -- Claude Desktop was running and simply never appeared.

    The package directory is looked up by prefix rather than hardcoded: the
    suffix after ``Claude_`` is a publisher hash, stable per publisher but not
    ours to assume. Falls back to the unredirected path so a non-Store install
    keeps working, and so the glob below has somewhere to look either way.
    """
    roaming = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    plain = roaming / "Claude"
    if plain.is_dir():
        return plain
    packages = Path(os.environ.get("LOCALAPPDATA")
                    or Path.home() / "AppData" / "Local") / "Packages"
    try:
        for package in sorted(packages.glob("Claude_*")):
            redirected = package / "LocalCache" / "Roaming" / "Claude"
            if redirected.is_dir():
                return redirected
    except OSError:
        pass
    return plain


APP_SUPPORT = (_windows_app_support() if sys.platform == "win32"
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
