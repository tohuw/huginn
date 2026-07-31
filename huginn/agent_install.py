"""Start-at-login install/uninstall so the daemon survives logout/reboot.

Same blast-radius discipline as hooks/install.py: back up before overwrite,
write via temp file + os.replace. Port-bind is the single-instance mutex, so a
supervisor racing a manual `huginn serve` is harmless (the loser just fails to
bind and exits).

Each OS gets a ``LoginAgent`` backend rather than platform branches sprinkled
through the install/uninstall flow -- issue #39, which also needs this shape
shared with a companion tool. The seam is deliberately separate from
``huginn.platform.get_platform()``: that selector maps Linux onto the macOS
adapter on purpose (Unix process/focus behaviour is close enough), which is
exactly the wrong answer for a login supervisor, where launchd and systemd
share nothing.

Restart policy differs per OS on purpose:

macOS
    launchd ``KeepAlive`` restarts the daemon even after a clean exit. That is
    intentionally incompatible with the menu-bar app owning the daemon
    lifecycle, so ``Huginn.app`` documents removing this agent first, and the
    behaviour is preserved here unchanged.
Linux
    systemd ``Restart=on-failure`` recovers from a crash but honours a
    deliberate stop. There is no Linux Huginn UI to fight over the lifecycle,
    so there is no reason to import launchd's known conflict.
Windows
    The tray app already supervises the daemon and owns its own start-at-login
    registration, so this installs a headless autostart only when the tray is
    not already claiming that job -- two supervisors is the same double-owner
    mistake as launchd versus Huginn.app.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from abc import ABC, abstractmethod
from pathlib import Path

LABEL = "is.tohuw.huginn"
PLIST_PATH = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
REPO_ROOT = Path(__file__).resolve().parent.parent
LOG_PATH = Path.home() / ".local" / "state" / "huginn" / "agent.log"

UNIT_NAME = "huginn.service"
SYSTEMD_USER_DIR = Path(
    os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
) / "systemd" / "user"
UNIT_PATH = SYSTEMD_USER_DIR / UNIT_NAME

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
# windows/Huginn.Tray writes "Huginn" for its own "Start at login" item. A
# separate value name keeps the two from silently overwriting each other.
TRAY_RUN_VALUE = "Huginn"
DAEMON_RUN_VALUE = "HuginnDaemon"

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

_UNIT_TEMPLATE = """[Unit]
Description=Huginn local AI coding-session monitor
Documentation=https://github.com/tohuw/huginn
After=default.target

[Service]
Type=simple
ExecStart={python} -m huginn.cli serve --no-open
WorkingDirectory={cwd}
# Recover from a crash, but honour a deliberate `systemctl --user stop`.
Restart=on-failure
RestartSec=2

[Install]
WantedBy=default.target
"""


def _plist_xml() -> str:
    return _PLIST_TEMPLATE.format(
        label=LABEL, python=sys.executable, cwd=REPO_ROOT, log=LOG_PATH)


def _unit_text() -> str:
    return _UNIT_TEMPLATE.format(python=sys.executable, cwd=REPO_ROOT)


def _write_with_backup(path: Path, text: str) -> None:
    """Back up any existing file, then publish the new one atomically."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        backup = path.with_name(path.name + f".huginn-bak.{int(time.time())}")
        backup.write_text(path.read_text())
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def _launchctl(*args: str) -> subprocess.CompletedProcess:
    if sys.platform != "darwin":
        raise RuntimeError("LaunchAgent management is only available on macOS")
    return subprocess.run(["launchctl", *args], capture_output=True, text=True)


def _systemctl(*args: str) -> subprocess.CompletedProcess:
    if not sys.platform.startswith("linux"):
        raise RuntimeError("systemd user units are only available on Linux")
    return subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True)


def _is_loaded() -> bool:
    return _launchctl("list", LABEL).returncode == 0


class LoginAgent(ABC):
    """One OS's start-at-login mechanism, behind a single narrow contract."""

    label: str

    @abstractmethod
    def installed(self) -> bool: ...

    @abstractmethod
    def install(self) -> int: ...

    @abstractmethod
    def uninstall(self) -> int: ...


class LaunchdAgent(LoginAgent):
    label = "LaunchAgent"

    def installed(self) -> bool:
        return PLIST_PATH.exists()

    def install(self) -> int:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        if _is_loaded():
            _launchctl("unload", "-w", str(PLIST_PATH))
        _write_with_backup(PLIST_PATH, _plist_xml())
        r = _launchctl("load", "-w", str(PLIST_PATH))
        if r.returncode != 0:
            print(f"launchctl load failed: {r.stderr.strip()}")
            return 1
        print(f"installed {PLIST_PATH}")
        print(f"huginn will now start at login and restart if it dies (log: {LOG_PATH})")
        return 0

    def uninstall(self) -> int:
        if not self.installed():
            print("not installed")
            return 0
        _launchctl("unload", "-w", str(PLIST_PATH))
        PLIST_PATH.unlink()
        print(f"removed {PLIST_PATH}")
        return 0


class SystemdUserAgent(LoginAgent):
    label = "systemd user unit"

    def installed(self) -> bool:
        return UNIT_PATH.exists()

    def install(self) -> int:
        _write_with_backup(UNIT_PATH, _unit_text())
        reload_result = _systemctl("daemon-reload")
        if reload_result.returncode != 0:
            print(f"systemctl daemon-reload failed: {reload_result.stderr.strip()}")
            return 1
        enable = _systemctl("enable", "--now", UNIT_NAME)
        if enable.returncode != 0:
            print(f"systemctl enable failed: {enable.stderr.strip()}")
            return 1
        print(f"installed {UNIT_PATH}")
        # Without lingering, a user unit stops at logout instead of surviving
        # it -- say so rather than let the difference be discovered later.
        print("huginn will now start when you log in and restart if it crashes "
              f"(log: journalctl --user -u {UNIT_NAME})")
        print("for a headless host, keep it running between logins with "
              "`loginctl enable-linger $USER`")
        return 0

    def uninstall(self) -> int:
        if not self.installed():
            print("not installed")
            return 0
        _systemctl("disable", "--now", UNIT_NAME)
        UNIT_PATH.unlink()
        _systemctl("daemon-reload")
        print(f"removed {UNIT_PATH}")
        return 0


def _winreg():
    """Indirection so the Windows backend stays testable off Windows."""
    import winreg
    return winreg


def _daemon_command() -> str:
    """Console-free autostart command line for the Run key.

    pythonw.exe when it is available: python.exe would leave a console window
    on screen for every login, which the tray shell already goes out of its way
    to avoid.
    """
    executable = Path(sys.executable)
    windowless = executable.with_name("pythonw.exe")
    if windowless.exists():
        executable = windowless
    return f'"{executable}" -m huginn.cli serve --no-open'


class WindowsStartupAgent(LoginAgent):
    label = "startup entry"

    @staticmethod
    def _value(name: str) -> str | None:
        winreg = _winreg()
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
                return str(winreg.QueryValueEx(key, name)[0])
        except OSError:
            return None

    def installed(self) -> bool:
        return self._value(DAEMON_RUN_VALUE) is not None

    def tray_owns_startup(self) -> bool:
        return self._value(TRAY_RUN_VALUE) is not None

    def install(self) -> int:
        if self.tray_owns_startup():
            # The tray starts, supervises, and stops the daemon. A second
            # autostart would resurrect a daemon the user just quit.
            print("huginn: the Windows tray app already starts huginn at login",
                  file=sys.stderr)
            print("huginn: uncheck its \"Start at login\" item first, or leave "
                  "startup to the tray", file=sys.stderr)
            return 1
        winreg = _winreg()
        command = _daemon_command()
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.SetValueEx(key, DAEMON_RUN_VALUE, 0, winreg.REG_SZ, command)
        print(rf"installed HKCU\{RUN_KEY}\{DAEMON_RUN_VALUE}")
        print(f"huginn will now start at login: {command}")
        # No supervisor here: the Run key starts a process once per login and
        # does not restart it. Don't imply otherwise.
        print("this starts huginn at login but does not restart it if it exits; "
              "the tray app supervises the daemon")
        return 0

    def uninstall(self) -> int:
        if not self.installed():
            print("not installed")
            return 0
        winreg = _winreg()
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                            winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, DAEMON_RUN_VALUE)
        print(rf"removed HKCU\{RUN_KEY}\{DAEMON_RUN_VALUE}")
        return 0


def get_login_agent(name: str | None = None) -> LoginAgent | None:
    """Backend for a platform name (defaults to the host), or None if unsupported."""
    selected = name or sys.platform
    if selected == "darwin":
        return LaunchdAgent()
    if selected.startswith("linux"):
        return SystemdUserAgent()
    if selected == "win32" or selected.startswith("cygwin"):
        return WindowsStartupAgent()
    return None


def _unsupported() -> int:
    print(f"huginn: start-at-login is not supported on {sys.platform}", file=sys.stderr)
    return 2


def install() -> int:
    agent = get_login_agent()
    return agent.install() if agent else _unsupported()


def uninstall() -> int:
    agent = get_login_agent()
    return agent.uninstall() if agent else _unsupported()
