"""Start-at-login install/uninstall so the daemon survives logout/reboot.

The ``LoginAgent`` seam and all three backends moved to ``corvidae.login_agent``
-- issue #42. Issue #39 built this shape explicitly with sharing in mind, and
Muninn now needs the same thing, so the mechanism lives in the shared package and
Huginn supplies only the parts that are Huginn's: the label, the argv, where the
log goes, and the Windows registry value the tray does *not* own.

Every name this module used to export is preserved below and the CLI verbs
``install-agent``/``uninstall-agent`` behave exactly as before;
``tests/test_shared_package_compat.py`` pins that, and ``tests/test_agent_install.py``
still exercises all three backends unchanged.

The three backend classes are the one place this file *subclasses* rather than
re-exporting, unlike ``huginn.model`` and ``huginn.sources.transcript``. The
reason is that they are the only shared objects that take construction arguments:
corvidae's take a ``LoginAgentSpec``, while Huginn's callers construct
``LaunchdAgent()`` with none and expect Huginn's own spec. The subclasses supply
it and route each OS boundary back through this module's ``_launchctl``/
``_systemctl``/``_winreg`` so those stay the one place to intercept. ``isinstance``
against ``corvidae.LoginAgent`` and each corvidae backend still holds, which is
the property a consumer could actually depend on.

The hardening came along unchanged and is documented at its new home:
``plistlib.dumps`` rather than an XML template, systemd rejecting ``\\n``
/``\\r``/``%``, ``_write_with_backup``'s symlink refusal and ``mkstemp`` +
0600-before-content discipline, launchd keeping ``KeepAlive`` while systemd uses
``Restart=on-failure``, and the Windows path refusing to install while the tray
owns startup.

Where it came from, stated precisely because the test files below cite it by
number: it is the output of a **security review of the surface #41 added**, not of
#41's own scope. #41 was "Plugin registry is purely additive: no way to express
'only these models may be used'" -- the model-policy chokepoint -- and the review
of that change swept this file too, which is where the ``C``/``H``/``M`` finding
ids in ``tests/test_agent_install.py`` come from. #39 is the issue that built this
mechanism ("No lag reporting for derived state, and background install is
launchd-only" -- it covered both halves). #43 contributed the teardown ordering,
not the file discipline.

Restart policy differs per OS on purpose:

macOS
    launchd ``KeepAlive`` restarts the daemon even after a clean exit. It is
    preserved unchanged, and the consequence is now a *documented* one rather than
    a conflict with a bundled app: the menu bar's **Quit** row (``raven.QUIT``)
    shuts the daemon down cleanly, and launchd starts it straight back up. Quitting
    for good means ``uninstall-agent`` first. That was already true of the deleted
    ``Huginn.app`` -- which documented the same thing -- so nothing about the policy
    changed when it went away.
Linux
    systemd ``Restart=on-failure`` recovers from a crash but honours a
    deliberate stop, so a **Quit** from the menu bar sticks here.
Windows
    Not restarted at all. This installs a headless autostart under
    ``DAEMON_RUN_VALUE`` and still refuses while ``TRAY_RUN_VALUE`` is present --
    see the note on that constant below: the tray it belonged to is gone, but the
    registry value it left on real machines is not.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from corvidae import login_agent as _shared
from corvidae.login_agent import (  # noqa: F401  -- re-export for import compatibility
    RUN_KEY,
    LoginAgent,
    LoginAgentSpec,
    _unlink_config,
)

LABEL = "is.tohuw.huginn"
REPO_ROOT = Path(__file__).resolve().parent.parent
LOG_PATH = Path.home() / ".local" / "state" / "huginn" / "agent.log"

UNIT_NAME = "huginn.service"

# The removed windows tray shell wrote "Huginn" for its own "Start at login"
# item. A separate value name kept the two from silently overwriting each other.
#
# Kept after that tray was deleted, deliberately. The code is gone from this
# repository; the registry value it wrote is still sitting in HKCU on every machine
# that ran it, and nothing removed it on the way out. Dropping this constant would
# make ``install-agent`` write ``HuginnDaemon`` beside a live ``Huginn`` autostart
# and produce two daemons at login -- the double-owner bug the guard exists to
# prevent, reintroduced by a cleanup. It costs one string to keep the refusal
# working for those users, and the refusal explains itself.
TRAY_RUN_VALUE = "Huginn"
DAEMON_RUN_VALUE = "HuginnDaemon"

# Where each OS's config lands. Module constants because that is what the CLI,
# the docs, and the tests all name; ``spec()`` reads them at call time.
PLIST_PATH = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
SYSTEMD_USER_DIR = Path(
    os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
) / "systemd" / "user"
UNIT_PATH = SYSTEMD_USER_DIR / UNIT_NAME


def spec() -> LoginAgentSpec:
    """Huginn's login-agent description.

    Built per call rather than at import, because every ingredient is a
    module-level name the tests (and a fork with a relocated checkout) patch:
    ``REPO_ROOT``, ``LOG_PATH``, ``PLIST_PATH``, ``UNIT_PATH``, and
    ``sys.executable``. A frozen module-level spec would snapshot all of them at
    import and quietly ignore every override -- exactly the failure that makes an
    install write to one developer's path (issue #37).
    """
    return LoginAgentSpec(
        name="huginn",
        label=LABEL,
        argv=[str(sys.executable), "-m", "huginn.cli", "serve", "--no-open"],
        working_dir=str(REPO_ROOT),
        log_path=LOG_PATH,
        description="Huginn local AI coding-session monitor",
        documentation="https://github.com/tohuw/huginn",
        plist_path=PLIST_PATH,
        unit_path=UNIT_PATH,
        registry_value=DAEMON_RUN_VALUE,
        tray_registry_value=TRAY_RUN_VALUE,
        # Worded for what a user can act on: the tray that wrote this value no
        # longer exists, so "the tray supervises the daemon" would be advice about
        # software they cannot find. What they need to know is that a leftover
        # autostart is in the way and removing it is the fix.
        tray_owner="a previously installed Huginn tray",
        windows_note="a removed tray app left its own startup entry behind",
        program_label="the Python executable",
        working_dir_label="the Huginn checkout path",
    )


def _plist_xml() -> str:
    """Huginn's launchd plist. Delegates; kept as a zero-argument name for tests."""
    return _shared._plist_xml(spec())


def _unit_text() -> str:
    """Huginn's systemd user unit. Delegates; kept as a zero-argument name."""
    return _shared._unit_text(spec())


def _daemon_command() -> str:
    """The console-free Windows Run-key command line for Huginn's daemon.

    A zero-argument delegate, because that is the signature this module has always
    exported. The pythonw.exe preference lives in corvidae now: python.exe would
    leave a console window on screen for every login, which the tray shell already
    goes out of its way to avoid.
    """
    return _shared._run_command(spec())


def _write_with_backup(path: Path, text: str) -> None:
    """Write a login-agent config with a 0600 backup. Delegates to corvidae.

    Kept as a two-argument name at this path, with the ``.huginn-bak.`` tag
    supplied here, because that tag is in every Huginn user's LaunchAgents
    directory and in the tests that verify the #41 H3 hardening.
    """
    _shared._write_with_backup(path, text, backup_tag=spec().backup_tag)


# ── The OS boundaries, kept here as the single place to intercept ─────────────
#
# These wrap corvidae's equivalents rather than being re-exported, because the
# platform guard has to read *this* module's ``sys``: every test patches
# ``agent_install.sys.platform``, and a re-export would read corvidae's instead
# and let a macOS-only call through on a Linux run.

def _launchctl(*args: str) -> subprocess.CompletedProcess:
    if sys.platform != "darwin":
        raise RuntimeError("LaunchAgent management is only available on macOS")
    return subprocess.run(["launchctl", *args], capture_output=True, text=True)


def _systemctl(*args: str) -> subprocess.CompletedProcess:
    if not sys.platform.startswith("linux"):
        raise RuntimeError("systemd user units are only available on Linux")
    return subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True)


def _winreg():
    """Indirection so the Windows backend stays testable off Windows."""
    import winreg
    return winreg


def _is_loaded() -> bool:
    return _launchctl("list", LABEL).returncode == 0


# ── The backends, bound to Huginn's spec and Huginn's boundaries ──────────────

class LaunchdAgent(_shared.LaunchdAgent):
    """corvidae's launchd backend, pre-loaded with Huginn's spec."""

    def __init__(self, agent_spec: LoginAgentSpec | None = None) -> None:
        super().__init__(agent_spec or spec())

    def launchctl(self, *args: str) -> subprocess.CompletedProcess:
        return _launchctl(*args)


class SystemdUserAgent(_shared.SystemdUserAgent):
    """corvidae's systemd backend, pre-loaded with Huginn's spec."""

    def __init__(self, agent_spec: LoginAgentSpec | None = None) -> None:
        super().__init__(agent_spec or spec())

    def systemctl(self, *args: str) -> subprocess.CompletedProcess:
        return _systemctl(*args)


class WindowsStartupAgent(_shared.WindowsStartupAgent):
    """corvidae's Windows backend, pre-loaded with Huginn's spec."""

    def __init__(self, agent_spec: LoginAgentSpec | None = None) -> None:
        super().__init__(agent_spec or spec())

    def registry(self):
        return _winreg()


def get_login_agent(name: str | None = None) -> LoginAgent | None:
    """Backend for a platform name (defaults to the host), or None if unsupported.

    Keeps the one-argument signature Huginn's callers and its CLI already use,
    and returns *Huginn's* subclasses so the boundaries above stay the seam.
    Returning None rather than raising is deliberate: "this OS has no
    start-at-login mechanism I know" is a thing to report with an exit code.
    """
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


__all__ = [
    "DAEMON_RUN_VALUE",
    "LABEL",
    "LOG_PATH",
    "PLIST_PATH",
    "REPO_ROOT",
    "RUN_KEY",
    "SYSTEMD_USER_DIR",
    "TRAY_RUN_VALUE",
    "UNIT_NAME",
    "UNIT_PATH",
    "LaunchdAgent",
    "LoginAgent",
    "LoginAgentSpec",
    "SystemdUserAgent",
    "WindowsStartupAgent",
    "get_login_agent",
    "install",
    "spec",
    "uninstall",
]
