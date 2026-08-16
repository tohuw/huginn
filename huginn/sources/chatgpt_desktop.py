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


#: Where a *packaged desktop app* lives, as opposed to a command-line tool of
#: the same name. Matched positively rather than denylisting CLI locations: a
#: GUI app has one recognisable install shape, while CLIs arrive in as many
#: layouts as there are installers -- this machine alone had ``codex.exe`` under
#: the npm tree *and* under ``AppData\Local\OpenAI\Codex\bin\<hash>``, and a
#: denylist would have to grow a rule for each.
_APP_MARKERS = {
    "win32": ("/windowsapps/openai.", "/programs/chatgpt/", "/programs/codex/"),
    "darwin": ("/contents/macos/",),
}


def _support_dirs() -> list[Path]:
    if sys.platform == "win32":
        roaming = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
        local = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        dirs = [roaming / "Codex", roaming / "ChatGPT", local / "Codex", local / "ChatGPT"]
        # The desktop app ships from the Store as the MSIX package
        # ``OpenAI.Codex``, and an MSIX package gets a redirected profile -- so
        # its "roaming appdata" is really under LocalCache. Nothing is written
        # to the unredirected paths above, so activity always read as zero and
        # the tile could never say anything but idle.
        try:
            for package in sorted((local / "Packages").glob("OpenAI.*")):
                cache = package / "LocalCache"
                dirs += [cache / "Roaming" / "Codex", cache / "Roaming" / "ChatGPT",
                         cache / "Local" / "Codex", cache / "Local" / "ChatGPT"]
        except OSError:
            pass
        return dirs
    base = Path.home() / "Library" / "Application Support"
    return [base / "Codex", base / "com.openai.codex", base / "OpenAI"]


def _is_desktop_app(pid: int) -> bool:
    """Is this pid the desktop app, rather than a CLI that shares its name?

    The name is not identity here. The Store package ``OpenAI.Codex`` ships
    *both* ``ChatGPT.exe`` and ``Codex.exe``, the npm CLI is also ``codex.exe``,
    and Windows matches process names case-insensitively -- so matching "Codex"
    put a "ChatGPT Desktop is running" tile on the roster whenever the *CLI*
    ran, and kept it there. The tile outlived the app by an hour and a half
    because the CLI it was really watching never stopped.

    A path that cannot be read counts as "not the app". That direction is
    deliberate and is the opposite of the usual best-effort rule: this source
    exists to say whether something is running, so a tile nobody can confirm is
    the failure being fixed, not a degradation worth preserving.
    """
    markers = _APP_MARKERS.get(sys.platform)
    if not markers:
        return False
    path = (_platform.process_path(pid) or "").replace("\\", "/").lower()
    return bool(path) and any(marker in path for marker in markers)


def _app_pids() -> list[int]:
    # Current desktop builds use ChatGPT as the main process; Codex is retained
    # because the Windows Store package is literally called OpenAI.Codex and
    # ships a Codex.exe beside ChatGPT.exe. Both names are then confirmed by
    # path, which is the only thing that separates the app from the CLI.
    named = set(_platform.find_processes("ChatGPT") + _platform.find_processes("Codex"))
    return sorted(pid for pid in named if _is_desktop_app(pid))


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
