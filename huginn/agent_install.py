"""LaunchAgent install/uninstall so the daemon survives logout/reboot.

Same blast-radius discipline as hooks/install.py: back up before overwrite,
write via temp file + os.replace. Port-bind is the single-instance mutex, so
launchd's KeepAlive racing a manual `huginn serve` is harmless (the loser
just fails to bind and exits).
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

LABEL = "is.tohuw.huginn"
PLIST_PATH = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
REPO_ROOT = Path(__file__).resolve().parent.parent
LOG_PATH = Path.home() / ".local" / "state" / "huginn" / "agent.log"

_PLIST_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{label}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{python}</string>
        <string>-m</string>
        <string>huginn.cli</string>
        <string>serve</string>
        <string>--no-open</string>
    </array>
    <key>WorkingDirectory</key>
    <string>{cwd}</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>{log}</string>
    <key>StandardErrorPath</key>
    <string>{log}</string>
</dict>
</plist>
"""


def _plist_xml() -> str:
    return _PLIST_TEMPLATE.format(
        label=LABEL, python=sys.executable, cwd=REPO_ROOT, log=LOG_PATH)


def _launchctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", *args], capture_output=True, text=True)


def _is_loaded() -> bool:
    return _launchctl("list", LABEL).returncode == 0


def install() -> int:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    PLIST_PATH.parent.mkdir(parents=True, exist_ok=True)

    if _is_loaded():
        _launchctl("unload", "-w", str(PLIST_PATH))

    if PLIST_PATH.exists():
        backup = PLIST_PATH.with_name(PLIST_PATH.name + f".huginn-bak.{int(time.time())}")
        backup.write_text(PLIST_PATH.read_text())

    tmp = PLIST_PATH.with_suffix(".plist.tmp")
    tmp.write_text(_plist_xml())
    tmp.replace(PLIST_PATH)

    r = _launchctl("load", "-w", str(PLIST_PATH))
    if r.returncode != 0:
        print(f"launchctl load failed: {r.stderr.strip()}")
        return 1
    print(f"installed {PLIST_PATH}")
    print(f"huginn will now start at login and restart if it dies (log: {LOG_PATH})")
    return 0


def uninstall() -> int:
    if PLIST_PATH.exists():
        _launchctl("unload", "-w", str(PLIST_PATH))
        PLIST_PATH.unlink()
        print(f"removed {PLIST_PATH}")
    else:
        print("not installed")
    return 0
