"""Start-at-login supervision, one backend per OS -- issue #39, shared via #42.

A project that runs a long-lived local daemon has to survive logout and reboot,
and the three ways to arrange that (launchd, systemd user units, the Windows Run
key) share nothing but their purpose. This module is that purpose behind one
contract: a :class:`LoginAgentSpec` says *what* to run and *where*, and
:func:`get_login_agent` returns the backend that knows *how* on the host.

Written for Huginn and extracted here when Muninn needed the same thing (issue
#42). The seam is deliberately separate from any "which OS am I on" selector a
consumer already has for process/focus work: those commonly map Linux onto a
Unix-ish adapter because process behaviour is close enough, which is exactly the
wrong answer for a login supervisor, where launchd and systemd share nothing.

Restart policy differs per OS on purpose, and the difference is the contract:

launchd
    ``KeepAlive`` restarts the daemon even after a clean exit. That is
    intentionally incompatible with a menu-bar/tray app owning the daemon
    lifecycle, so a consumer that ships one must document removing this agent
    first. It is preserved rather than softened because a supervisor that gives
    up on a crash is not a supervisor.
systemd
    ``Restart=on-failure`` recovers from a crash but honours a deliberate
    ``systemctl --user stop``. Importing launchd's known conflict here would
    break the one command a Linux user reaches for.
Windows
    The Run key starts a process once per login and never restarts it. If a tray
    app already registers its own start-at-login entry, installing a second
    headless autostart is the same double-owner mistake as launchd versus a
    menu-bar app, so :class:`WindowsStartupAgent` refuses instead.

Every value that reaches persistent auto-start config is treated as hostile,
because a filesystem path is attacker-influenceable and this config is executed
at every login. See :func:`_plist_xml` and :func:`_systemd_value` for what that
cost before it was fixed (issue #41).
"""
from __future__ import annotations

import os
import plistlib
import subprocess
import sys
import tempfile
import time
from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

# O_NOFOLLOW exists on every platform whose backend actually writes a file; on
# Windows the login agent is the registry, which never reaches this code.
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)

#: Where the Windows per-user autostart entries live.
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


@dataclass(frozen=True)
class LoginAgentSpec:
    """One project's answer to "what should start at login, and how is it named".

    Only ``name``, ``label``, ``argv``, ``working_dir`` and ``log_path`` are
    required; everything else has a default that is either derived from those or
    cosmetic. New optional fields may be added within a CalVer year, so construct
    this with keywords rather than positionally past the required five.

    ``argv`` is the whole command, interpreter first. It is not a shell string:
    launchd takes it as ``ProgramArguments`` and the systemd unit quotes each
    element that needs it, so nothing here is ever word-split by a shell.
    """

    #: Short lowercase project name. Names the systemd unit (``{name}.service``),
    #: the ``.{name}-bak.<ts>`` backup files, and the user-facing messages.
    name: str
    #: Reverse-DNS launchd label, e.g. ``is.tohuw.huginn``. Also the plist
    #: filename when ``plist_path`` is not given.
    label: str
    #: The command to run, interpreter first.
    argv: Sequence[str]
    #: ``WorkingDirectory`` for both launchd and systemd.
    working_dir: str
    #: launchd's ``StandardOutPath``/``StandardErrorPath``. systemd uses the
    #: journal instead, so this is macOS-only.
    log_path: Path

    # ── systemd unit metadata ────────────────────────────────────────────────
    description: str = ""
    documentation: str = ""

    # ── explicit locations, when the derived default is not wanted ───────────
    plist_path: Path | None = None
    unit_path: Path | None = None

    # ── Windows ──────────────────────────────────────────────────────────────
    #: The ``HKCU\\...\\Run`` value name this agent owns. Deliberately distinct
    #: from ``tray_registry_value`` so the two cannot silently overwrite each
    #: other.
    registry_value: str = ""
    #: The Run value a tray app writes for its own "Start at login" item. When it
    #: is present, install refuses. Empty means the consumer ships no tray.
    tray_registry_value: str = ""
    #: How the refusal names that tray, and an optional extra line about who
    #: does supervise the daemon on Windows.
    tray_owner: str = "Windows tray app"
    windows_note: str = ""

    # ── error wording ────────────────────────────────────────────────────────
    # The systemd validator refuses a value it cannot represent, and the user has
    # to know *which* path to move. "the program path" is accurate but a consumer
    # usually knows something better ("the Python executable").
    program_label: str = "the program path"
    working_dir_label: str = "the working directory"

    @property
    def plist(self) -> Path:
        """The LaunchAgents plist location."""
        if self.plist_path is not None:
            return Path(self.plist_path)
        return Path.home() / "Library" / "LaunchAgents" / f"{self.label}.plist"

    @property
    def unit_name(self) -> str:
        return f"{self.name}.service"

    @property
    def unit(self) -> Path:
        """The systemd user-unit location, honouring ``$XDG_CONFIG_HOME``."""
        if self.unit_path is not None:
            return Path(self.unit_path)
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
        return base / "systemd" / "user" / self.unit_name

    @property
    def run_value(self) -> str:
        return self.registry_value or f"{self.name.capitalize()}Daemon"

    @property
    def backup_tag(self) -> str:
        """Infix for a backup file, e.g. ``huginn.service.huginn-bak.1700000000``."""
        return f"{self.name}-bak"


# ── Serializing the config ────────────────────────────────────────────────────

def _plist_xml(spec: LoginAgentSpec) -> str:
    """The launchd plist, serialized by ``plistlib`` rather than string-formatted.

    issue #41 C3: this was a ``str.format`` into XML with zero escaping, and the
    interpolated values are a filesystem path and ``sys.executable``. A directory
    name containing XML injected arbitrary keys into *persistent auto-start
    config*: an argv payload became extra ``ProgramArguments`` (verified:
    ``['-c', '__import__("os").system(...)']``), and a working-directory payload
    added a whole ``EnvironmentVariables``/``DYLD_INSERT_LIBRARIES`` dict that
    ``plistlib`` parses cleanly. ``KeepAlive`` then relaunches it forever. "My
    first payload broke the XML" is not a mitigation; ``plistlib.dumps`` of a real
    dict is, because there is no longer a text template to escape out of.
    """
    return plistlib.dumps({
        "Label": spec.label,
        "ProgramArguments": [str(arg) for arg in spec.argv],
        "WorkingDirectory": str(spec.working_dir),
        "RunAtLoad": True,
        # Deliberately conflicts with an app that owns the daemon lifecycle --
        # see the module docstring. Preserved rather than softened.
        "KeepAlive": True,
        "StandardOutPath": str(spec.log_path),
        "StandardErrorPath": str(spec.log_path),
    }).decode()


def _systemd_value(name: str, value: str, *, quote: bool = True) -> str:
    """One systemd unit value, or a clear error if it cannot be represented.

    issue #41 C3: the unit was a ``str.format`` too, so a newline in the
    working-directory path injected arbitrary directives -- verified with an
    ``ExecStartPre=/bin/sh -c ...`` line landing in a persistent user unit.

    ``\\n`` and ``\\r`` end a directive and so cannot be represented at all;
    ``%`` is systemd's specifier prefix (``%h``, ``%t``, ...) and would expand at
    load time into something other than the value meant. Rejecting is right
    rather than escaping: these are an executable path, a checkout location, and
    fixed unit metadata, so a value containing them is a broken install to be
    reported, not a case to accommodate. ``systemd.syntax`` double-quoting covers
    the rest.

    ``quote=False`` is for a directive that is a whole line rather than a word
    (``Description=``, ``Documentation=``): the validation is the security
    property and applies either way, while quoting a description would put the
    quotes on screen in ``systemctl status``.
    """
    for forbidden, why in (("\n", "a newline"), ("\r", "a carriage return"),
                           ("%", "a '%' specifier prefix")):
        if forbidden in value:
            raise ValueError(
                f"cannot write a systemd unit: {name} contains {why} ({value!r}). "
                "Move it to a value without it, or install the daemon some other way."
            )
    if not quote:
        return value
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _needs_quoting(word: str) -> bool:
    """True if a systemd ``ExecStart`` word would be mangled unquoted.

    Unquoted words are split on whitespace, so only whitespace and the quoting
    characters themselves matter. Quoting conditionally rather than always keeps
    an ``ExecStart`` readable -- ``"/usr/bin/python" -m pkg serve`` rather than
    every flag in its own quotes -- while a path with a space is still safe.
    """
    return any(c.isspace() for c in word) or any(c in word for c in "\"'\\")


_UNIT_TEMPLATE = """[Unit]
{unit_meta}After=default.target

[Service]
Type=simple
ExecStart={exec_start}
WorkingDirectory={working_dir}
# Recover from a crash, but honour a deliberate `systemctl --user stop`.
Restart=on-failure
RestartSec=2

[Install]
WantedBy=default.target
"""


def _unit_text(spec: LoginAgentSpec) -> str:
    """Render the systemd user unit for ``spec``, validating every value."""
    meta = ""
    if spec.description:
        meta += f"Description={_systemd_value('the unit description', spec.description, quote=False)}\n"
    if spec.documentation:
        meta += f"Documentation={_systemd_value('the unit documentation', spec.documentation, quote=False)}\n"

    argv = [str(arg) for arg in spec.argv]
    # argv[0] is a filesystem path and is always quoted; the rest are the
    # consumer's own literal arguments and are quoted only when they would
    # otherwise be word-split. Every element is validated either way.
    words = [_systemd_value(spec.program_label, argv[0])]
    for index, word in enumerate(argv[1:], start=1):
        words.append(_systemd_value(f"argument {index} ({word!r})", word,
                                    quote=_needs_quoting(word)))

    return _UNIT_TEMPLATE.format(
        unit_meta=meta,
        exec_start=" ".join(words),
        working_dir=_systemd_value(spec.working_dir_label, str(spec.working_dir)),
    )


# ── Publishing it ─────────────────────────────────────────────────────────────

def _write_with_backup(path: Path, text: str, *, backup_tag: str = "corvidae") -> None:
    """Back up any existing file, then publish the new one atomically.

    issue #41 H3, all of which was verified against the previous version:

    * A **symlinked target** made the backup read through the link and write a
      0600 secret into a 0644 ``.<tag>.<ts>`` file, and ``os.replace`` would then
      publish over whatever the link named. Refused outright: this function
      writes launchd/systemd config, and that is never a symlink in a healthy
      install.
    * A **pre-planted ``<name>.tmp`` symlink** was written *through*, and
      ``os.replace`` then made the unit path itself an attacker-owned symlink.
      ``mkstemp`` gives an unpredictable name and ``O_CREAT|O_EXCL|O_NOFOLLOW``
      semantics, so there is nothing to pre-plant and nothing to follow.
    * A fresh plist/unit was **0644** at the default umask, and a backup of a
      0600 file became 0644. Both are 0600 now, matching what a consumer already
      does for its token and state files.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise ValueError(
            f"refusing to write {path}: it is a symlink. A login-agent config "
            "file is never a symlink in a healthy install, and following one "
            "would publish this content wherever it points."
        )
    if path.exists():
        backup = path.with_name(path.name + f".{backup_tag}.{int(time.time())}")
        # 0600 *before* any content lands: the file being copied may itself be
        # secret, and a world-readable backup of it is the leak.
        with open(os.open(backup, os.O_CREAT | os.O_EXCL | os.O_WRONLY | _NOFOLLOW, 0o600),
                  "w") as handle:
            handle.write(path.read_text())
    # mkstemp: unpredictable name, O_CREAT|O_EXCL|O_NOFOLLOW, mode 0600.
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with open(fd, "w") as handle:
            handle.write(text)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _unlink_config(path: Path) -> None:
    """Remove a login-agent config file, refusing to follow a symlink.

    issue #41 H3: uninstall unlinked the unit path directly, and that path
    follows ``$XDG_CONFIG_HOME``. ``Path.unlink`` removes the link rather than
    its target, so the original was not an arbitrary-deletion primitive -- but a
    symlink here means the *installed* agent was not the file we think it was,
    which is worth refusing loudly rather than quietly tidying away.
    """
    if path.is_symlink():
        raise ValueError(
            f"refusing to act on {path}: it is a symlink, not the login-agent "
            "config this installed. Remove it by hand after checking where it points."
        )
    path.unlink()


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


# ── The backends ──────────────────────────────────────────────────────────────

class LoginAgent(ABC):
    """One OS's start-at-login mechanism, behind a single narrow contract.

    ``installed``/``install``/``uninstall`` is the whole surface. ``install`` and
    ``uninstall`` return a process exit code and print their own diagnosis,
    because "installed, but systemd could not enable it" is a partial success the
    caller cannot describe better than the backend can.

    Each backend also exposes its OS boundary as one overridable method
    (:meth:`LaunchdAgent.launchctl`, :meth:`SystemdUserAgent.systemctl`,
    :meth:`WindowsStartupAgent.registry`). That is not incidental: launchd,
    systemd, and the Windows registry are all absent from at least one machine a
    consumer develops on, and an instance-method seam is what lets all three
    backends be driven from any host. Overriding one is a supported thing to do.
    """

    #: Human name for this mechanism, for a caller that wants to say what it did.
    label: str

    def __init__(self, spec: LoginAgentSpec) -> None:
        self.spec = spec

    @abstractmethod
    def installed(self) -> bool: ...

    @abstractmethod
    def install(self) -> int: ...

    @abstractmethod
    def uninstall(self) -> int: ...


class LaunchdAgent(LoginAgent):
    label = "LaunchAgent"

    def launchctl(self, *args: str) -> subprocess.CompletedProcess:
        """Run ``launchctl``. Override to drive this backend off macOS."""
        return _launchctl(*args)

    def _is_loaded(self) -> bool:
        return self.launchctl("list", self.spec.label).returncode == 0

    def installed(self) -> bool:
        return self.spec.plist.exists()

    def install(self) -> int:
        spec = self.spec
        Path(spec.log_path).parent.mkdir(parents=True, exist_ok=True)
        if self._is_loaded():
            self.launchctl("unload", "-w", str(spec.plist))
        _write_with_backup(spec.plist, _plist_xml(spec), backup_tag=spec.backup_tag)
        r = self.launchctl("load", "-w", str(spec.plist))
        if r.returncode != 0:
            print(f"launchctl load failed: {r.stderr.strip()}")
            return 1
        print(f"installed {spec.plist}")
        print(f"{spec.name} will now start at login and restart if it dies "
              f"(log: {spec.log_path})")
        return 0

    def uninstall(self) -> int:
        if not self.installed():
            print("not installed")
            return 0
        self.launchctl("unload", "-w", str(self.spec.plist))
        _unlink_config(self.spec.plist)
        print(f"removed {self.spec.plist}")
        return 0


class SystemdUserAgent(LoginAgent):
    label = "systemd user unit"

    def systemctl(self, *args: str) -> subprocess.CompletedProcess:
        """Run ``systemctl --user``. Override to drive this backend off Linux."""
        return _systemctl(*args)

    def installed(self) -> bool:
        return self.spec.unit.exists()

    def install(self) -> int:
        spec = self.spec
        _write_with_backup(spec.unit, _unit_text(spec), backup_tag=spec.backup_tag)
        reload_result = self.systemctl("daemon-reload")
        if reload_result.returncode != 0:
            print(f"systemctl daemon-reload failed: {reload_result.stderr.strip()}")
            return 1
        enable = self.systemctl("enable", "--now", spec.unit_name)
        if enable.returncode != 0:
            print(f"systemctl enable failed: {enable.stderr.strip()}")
            return 1
        print(f"installed {spec.unit}")
        # Without lingering, a user unit stops at logout instead of surviving
        # it -- say so rather than let the difference be discovered later.
        print(f"{spec.name} will now start when you log in and restart if it crashes "
              f"(log: journalctl --user -u {spec.unit_name})")
        print("for a headless host, keep it running between logins with "
              "`loginctl enable-linger $USER`")
        return 0

    def uninstall(self) -> int:
        if not self.installed():
            print("not installed")
            return 0
        self.systemctl("disable", "--now", self.spec.unit_name)
        _unlink_config(self.spec.unit)
        self.systemctl("daemon-reload")
        print(f"removed {self.spec.unit}")
        return 0


def _run_command(spec: LoginAgentSpec) -> str:
    """Console-free autostart command line for the Run key.

    pythonw.exe when a sibling of ``argv[0]`` by that name exists: python.exe
    would leave a console window on screen for every login, which a tray shell
    already goes out of its way to avoid. A non-Python ``argv[0]`` simply has no
    such sibling, so this is a no-op for it rather than a special case.
    """
    executable = Path(str(spec.argv[0]))
    windowless = executable.with_name("pythonw.exe")
    if windowless.exists():
        executable = windowless
    rest = " ".join(str(arg) for arg in spec.argv[1:])
    return f'"{executable}" {rest}' if rest else f'"{executable}"'


class WindowsStartupAgent(LoginAgent):
    label = "startup entry"

    def registry(self):
        """Return the ``winreg`` module. Override to drive this backend off Windows."""
        return _winreg()

    def _value(self, name: str) -> str | None:
        winreg = self.registry()
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
                return str(winreg.QueryValueEx(key, name)[0])
        except OSError:
            return None

    def installed(self) -> bool:
        return self._value(self.spec.run_value) is not None

    def tray_owns_startup(self) -> bool:
        """True if a tray app has already claimed start-at-login.

        False without touching the registry when the consumer ships no tray:
        there is no value name to look for, and inventing one would make an
        unrelated key's presence refuse a valid install.
        """
        if not self.spec.tray_registry_value:
            return False
        return self._value(self.spec.tray_registry_value) is not None

    def install(self) -> int:
        spec = self.spec
        if self.tray_owns_startup():
            # The tray starts, supervises, and stops the daemon. A second
            # autostart would resurrect a daemon the user just quit.
            print(f"{spec.name}: the {spec.tray_owner} already starts {spec.name} at login",
                  file=sys.stderr)
            print(f"{spec.name}: uncheck its \"Start at login\" item first, or leave "
                  "startup to the tray", file=sys.stderr)
            return 1
        winreg = self.registry()
        command = _run_command(spec)
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.SetValueEx(key, spec.run_value, 0, winreg.REG_SZ, command)
        print(rf"installed HKCU\{RUN_KEY}\{spec.run_value}")
        print(f"{spec.name} will now start at login: {command}")
        # No supervisor here: the Run key starts a process once per login and
        # does not restart it. Don't imply otherwise.
        note = f"; {spec.windows_note}" if spec.windows_note else ""
        print(f"this starts {spec.name} at login but does not restart it if it exits{note}")
        return 0

    def uninstall(self) -> int:
        if not self.installed():
            print("not installed")
            return 0
        winreg = self.registry()
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                            winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, self.spec.run_value)
        print(rf"removed HKCU\{RUN_KEY}\{self.spec.run_value}")
        return 0


def get_login_agent(spec: LoginAgentSpec, name: str | None = None) -> LoginAgent | None:
    """Backend for a platform name (defaults to the host), or None if unsupported.

    Returning None rather than raising is deliberate: "this OS has no
    start-at-login mechanism I know" is a thing to report to the user with an
    exit code, not an exception to unwind through a CLI.
    """
    selected = name or sys.platform
    if selected == "darwin":
        return LaunchdAgent(spec)
    if selected.startswith("linux"):
        return SystemdUserAgent(spec)
    if selected == "win32" or selected.startswith("cygwin"):
        return WindowsStartupAgent(spec)
    return None


__all__ = [
    "RUN_KEY",
    "LaunchdAgent",
    "LoginAgent",
    "LoginAgentSpec",
    "SystemdUserAgent",
    "WindowsStartupAgent",
    "get_login_agent",
]
