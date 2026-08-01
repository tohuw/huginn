"""The shared start-at-login backends -- issue #39, extracted in #42.

Every backend is driven from any host by overriding its one OS boundary
(``launchctl``/``systemctl``/``registry``), so launchd, systemd, and the Windows
Run key are all covered on one machine without requiring those OSes. That the
boundary is overridable is itself part of the contract; see ``LoginAgent``.

The injection tests are the ones to read first. Each payload here was verified to
work against the pre-#41 version of this code: a plist built by string formatting
and a unit built by ``str.format`` both accepted arbitrary directives out of a
filesystem path, into config that runs at every login and that ``KeepAlive``
relaunches forever.
"""
from __future__ import annotations

import io
import os
import plistlib
import stat
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from corvidae import login_agent
from corvidae.login_agent import (
    LaunchdAgent,
    LoginAgentSpec,
    SystemdUserAgent,
    WindowsStartupAgent,
    get_login_agent,
)


def _ok(stdout: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], 0, stdout, "")


def _fail(stderr: str = "boom") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], 1, "", stderr)


def spec(**overrides) -> LoginAgentSpec:
    """A representative consumer's spec. Two projects, one shape."""
    fields = {
        "name": "testraven",
        "label": "is.example.testraven",
        "argv": ["/usr/bin/python3", "-m", "testraven.cli", "serve", "--no-open"],
        "working_dir": "/opt/testraven",
        "log_path": Path("/tmp/testraven/agent.log"),
        "description": "Test raven",
        "documentation": "https://example.invalid/testraven",
    }
    fields.update(overrides)
    return LoginAgentSpec(**fields)


class _RecordingLaunchd(LaunchdAgent):
    """launchd with its subprocess boundary replaced. ``loaded`` drives `list`."""

    def __init__(self, agent_spec, *, loaded: bool = False, fail: str | None = None):
        super().__init__(agent_spec)
        self.calls: list[tuple[str, ...]] = []
        self.loaded = loaded
        self.fail = fail

    def launchctl(self, *args: str) -> subprocess.CompletedProcess:
        self.calls.append(args)
        if args[0] == "list":
            return _ok() if self.loaded else _fail()
        return _fail("permission denied") if args[0] == self.fail else _ok()


class _RecordingSystemd(SystemdUserAgent):
    def __init__(self, agent_spec, *, results=None):
        super().__init__(agent_spec)
        self.calls: list[tuple[str, ...]] = []
        self.results = list(results or [])

    def systemctl(self, *args: str) -> subprocess.CompletedProcess:
        self.calls.append(args)
        return self.results.pop(0) if self.results else _ok()


class _FakeKey:
    def __init__(self, store: dict, name: str):
        self.store = store
        self.name = name

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class _FakeWinreg:
    """Minimal HKCU\\...\\Run stand-in so Windows is testable off Windows."""

    HKEY_CURRENT_USER = object()
    REG_SZ = 1
    KEY_SET_VALUE = 2

    def __init__(self, values: dict[str, str] | None = None, key_exists: bool = True):
        self.values = dict(values or {})
        self.key_exists = key_exists

    def OpenKey(self, root, path, reserved=0, access=0):
        if not self.key_exists:
            raise FileNotFoundError(path)
        return _FakeKey(self.values, path)

    def CreateKey(self, root, path):
        self.key_exists = True
        return _FakeKey(self.values, path)

    def QueryValueEx(self, key, name):
        if name not in self.values:
            raise FileNotFoundError(name)
        return (self.values[name], self.REG_SZ)

    def SetValueEx(self, key, name, reserved, kind, value):
        self.values[name] = value

    def DeleteValue(self, key, name):
        del self.values[name]


class _FakeWindows(WindowsStartupAgent):
    def __init__(self, agent_spec, winreg: _FakeWinreg):
        super().__init__(agent_spec)
        self.winreg = winreg

    def registry(self):
        return self.winreg


# ── The spec ──────────────────────────────────────────────────────────────────

class SpecTests(unittest.TestCase):
    def test_locations_derive_from_the_name_and_label(self):
        s = spec()
        self.assertEqual(s.plist.name, "is.example.testraven.plist")
        self.assertEqual(s.plist.parent, Path.home() / "Library" / "LaunchAgents")
        self.assertEqual(s.unit_name, "testraven.service")
        self.assertEqual(s.backup_tag, "testraven-bak")

    def test_unit_path_follows_xdg_config_home(self):
        with patch.dict(os.environ, {"XDG_CONFIG_HOME": "/tmp/cfg"}):
            self.assertEqual(spec().unit, Path("/tmp/cfg/systemd/user/testraven.service"))

    def test_explicit_paths_win_over_the_derived_ones(self):
        # A consumer whose config already lives somewhere must be able to say so
        # rather than have this module relocate its users' installed agents.
        s = spec(plist_path=Path("/tmp/x.plist"), unit_path=Path("/tmp/x.service"))
        self.assertEqual(s.plist, Path("/tmp/x.plist"))
        self.assertEqual(s.unit, Path("/tmp/x.service"))

    def test_registry_value_defaults_to_a_distinct_daemon_name(self):
        # Distinct from any tray value by construction: two supervisors silently
        # overwriting one Run value is the failure this naming avoids.
        self.assertEqual(spec().run_value, "TestravenDaemon")
        self.assertEqual(spec(registry_value="Explicit").run_value, "Explicit")


# ── launchd ───────────────────────────────────────────────────────────────────

class LaunchdAgentTests(unittest.TestCase):
    def test_keepalive_stays_in_the_plist(self):
        # A menu-bar app owning the daemon lifecycle conflicts with KeepAlive, and
        # a consumer that ships one documents removing this agent first. The
        # incompatibility must remain real, not silently papered over by dropping
        # KeepAlive -- a supervisor that gives up on a crash is not a supervisor.
        xml = login_agent._plist_xml(spec())
        self.assertIn("<key>KeepAlive</key>", xml)
        self.assertIn("<key>RunAtLoad</key>", xml)
        self.assertIn("testraven.cli", xml)

    def test_the_plist_says_what_the_spec_says(self):
        parsed = plistlib.loads(login_agent._plist_xml(spec()).encode())
        self.assertEqual(parsed["Label"], "is.example.testraven")
        self.assertEqual(parsed["ProgramArguments"], list(spec().argv))
        self.assertEqual(parsed["WorkingDirectory"], "/opt/testraven")
        self.assertEqual(parsed["StandardOutPath"], "/tmp/testraven/agent.log")
        self.assertEqual(parsed["StandardErrorPath"], parsed["StandardOutPath"])

    def test_install_backs_up_an_existing_plist_and_loads_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            plist = Path(tmp) / "is.example.testraven.plist"
            plist.write_text("<plist>old</plist>")
            agent = _RecordingLaunchd(
                spec(plist_path=plist, log_path=Path(tmp) / "agent.log"), loaded=True)

            with redirect_stdout(io.StringIO()) as out:
                self.assertEqual(agent.install(), 0)

            self.assertIn("<key>KeepAlive</key>", plist.read_text())
            backups = list(Path(tmp).glob("*.testraven-bak.*"))
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_text(), "<plist>old</plist>")
            # Unloaded first when already loaded, then loaded again.
            self.assertEqual(agent.calls[0], ("list", "is.example.testraven"))
            self.assertEqual(agent.calls[1], ("unload", "-w", str(plist)))
            self.assertEqual(agent.calls[-1], ("load", "-w", str(plist)))
            self.assertFalse(list(Path(tmp).glob("*.tmp")))
            self.assertIn("start at login", out.getvalue())

    def test_install_does_not_unload_what_was_never_loaded(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = _RecordingLaunchd(
                spec(plist_path=Path(tmp) / "a.plist", log_path=Path(tmp) / "agent.log"))
            with redirect_stdout(io.StringIO()):
                self.assertEqual(agent.install(), 0)
            self.assertNotIn("unload", [c[0] for c in agent.calls])

    def test_install_reports_a_failed_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = _RecordingLaunchd(
                spec(plist_path=Path(tmp) / "a.plist", log_path=Path(tmp) / "agent.log"),
                fail="load")
            with redirect_stdout(io.StringIO()) as out:
                self.assertEqual(agent.install(), 1)
            self.assertIn("launchctl load failed", out.getvalue())
            self.assertIn("permission denied", out.getvalue())

    def test_install_creates_the_log_directory(self):
        # launchd refuses to start a job whose StandardOutPath parent is missing.
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "state" / "deep" / "agent.log"
            agent = _RecordingLaunchd(spec(plist_path=Path(tmp) / "a.plist", log_path=log))
            with redirect_stdout(io.StringIO()):
                agent.install()
            self.assertTrue(log.parent.is_dir())

    def test_uninstall_unloads_and_removes_the_plist(self):
        with tempfile.TemporaryDirectory() as tmp:
            plist = Path(tmp) / "a.plist"
            plist.write_text("<plist/>")
            agent = _RecordingLaunchd(spec(plist_path=plist))
            with redirect_stdout(io.StringIO()) as out:
                self.assertEqual(agent.uninstall(), 0)
            self.assertFalse(plist.exists())
            self.assertEqual(agent.calls, [("unload", "-w", str(plist))])
            self.assertIn("removed", out.getvalue())

    def test_uninstall_is_a_no_op_when_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = _RecordingLaunchd(spec(plist_path=Path(tmp) / "none.plist"))
            with redirect_stdout(io.StringIO()) as out:
                self.assertEqual(agent.uninstall(), 0)
            self.assertEqual(agent.calls, [])
            self.assertIn("not installed", out.getvalue())

    def test_launchctl_itself_is_guarded_off_macos(self):
        with patch.object(login_agent.sys, "platform", "win32"):
            with self.assertRaisesRegex(RuntimeError, "only available on macOS"):
                login_agent._launchctl("list")


# ── systemd ───────────────────────────────────────────────────────────────────

class SystemdUserAgentTests(unittest.TestCase):
    def test_unit_restarts_on_failure_but_honours_a_deliberate_stop(self):
        # Not KeepAlive's semantics on purpose: `systemctl --user stop` must stay
        # effective, and there is no reason to import launchd's known conflict.
        unit = login_agent._unit_text(spec())
        self.assertIn("Restart=on-failure", unit)
        self.assertNotIn("Restart=always", unit)
        self.assertIn("WantedBy=default.target", unit)
        self.assertIn("-m testraven.cli serve --no-open", unit)

    def test_unit_metadata_is_unquoted_prose(self):
        # Description shows up in `systemctl status`, so quoting it would put the
        # quotes on the user's screen. Still validated, just not quoted.
        unit = login_agent._unit_text(spec())
        self.assertIn("Description=Test raven\n", unit)
        self.assertIn("Documentation=https://example.invalid/testraven\n", unit)

    def test_absent_metadata_emits_no_empty_directive(self):
        unit = login_agent._unit_text(spec(description="", documentation=""))
        self.assertNotIn("Description=", unit)
        self.assertNotIn("Documentation=", unit)

    def test_install_writes_the_unit_then_reloads_and_enables_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            unit = Path(tmp) / "systemd" / "user" / "testraven.service"
            agent = _RecordingSystemd(spec(unit_path=unit))
            with redirect_stdout(io.StringIO()) as out:
                self.assertEqual(agent.install(), 0)

            self.assertIn("Restart=on-failure", unit.read_text())
            self.assertEqual(agent.calls,
                             [("daemon-reload",), ("enable", "--now", "testraven.service")])
            # A user unit stops at logout without lingering -- surface it.
            self.assertIn("enable-linger", out.getvalue())

    def test_install_backs_up_an_existing_unit(self):
        with tempfile.TemporaryDirectory() as tmp:
            unit = Path(tmp) / "testraven.service"
            unit.write_text("[Service]\nExecStart=/old\n")
            with redirect_stdout(io.StringIO()):
                self.assertEqual(_RecordingSystemd(spec(unit_path=unit)).install(), 0)
            backups = list(Path(tmp).glob("testraven.service.testraven-bak.*"))
            self.assertEqual(len(backups), 1)
            self.assertIn("ExecStart=/old", backups[0].read_text())
            self.assertFalse(list(Path(tmp).glob("*.tmp")))

    def test_install_reports_a_failed_enable(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = _RecordingSystemd(spec(unit_path=Path(tmp) / "u.service"),
                                      results=[_ok(), _fail("unit is masked")])
            with redirect_stdout(io.StringIO()) as out:
                self.assertEqual(agent.install(), 1)
            self.assertIn("systemctl enable failed", out.getvalue())
            self.assertIn("unit is masked", out.getvalue())

    def test_install_reports_a_failed_daemon_reload(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = _RecordingSystemd(spec(unit_path=Path(tmp) / "u.service"),
                                      results=[_fail("no dbus")])
            with redirect_stdout(io.StringIO()) as out:
                self.assertEqual(agent.install(), 1)
            self.assertIn("daemon-reload failed", out.getvalue())

    def test_uninstall_disables_removes_and_reloads(self):
        with tempfile.TemporaryDirectory() as tmp:
            unit = Path(tmp) / "testraven.service"
            unit.write_text("[Service]\n")
            agent = _RecordingSystemd(spec(unit_path=unit))
            with redirect_stdout(io.StringIO()) as out:
                self.assertEqual(agent.uninstall(), 0)
            self.assertFalse(unit.exists())
            self.assertEqual(agent.calls, [("disable", "--now", "testraven.service"),
                                           ("daemon-reload",)])
            self.assertIn("removed", out.getvalue())

    def test_uninstall_is_a_no_op_when_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = _RecordingSystemd(spec(unit_path=Path(tmp) / "gone.service"))
            with redirect_stdout(io.StringIO()) as out:
                self.assertEqual(agent.uninstall(), 0)
            self.assertEqual(agent.calls, [])
            self.assertIn("not installed", out.getvalue())

    def test_systemctl_itself_is_guarded_off_linux(self):
        with patch.object(login_agent.sys, "platform", "darwin"):
            with self.assertRaisesRegex(RuntimeError, "only available on Linux"):
                login_agent._systemctl("daemon-reload")


# ── Injection ─────────────────────────────────────────────────────────────────

class ConfigInjectionTests(unittest.TestCase):
    """issue #41 C3: the plist and the unit were both ``str.format`` into
    structured config with zero escaping, and the interpolated values are a
    filesystem path and ``sys.executable``. A directory name containing XML
    (macOS) or a newline (Linux) injected arbitrary directives into *persistent
    auto-start config* that ``KeepAlive`` then relaunches forever. A payload
    erroring out is not a mitigation: the ones below parsed cleanly before the
    fix."""

    # Closes WorkingDirectory's <string>, adds a whole key/value pair, reopens a
    # string so the document stays balanced. Verified to parse cleanly and yield
    # a live DYLD_INSERT_LIBRARIES against the pre-fix template.
    XML_PAYLOAD = ("/tmp/x</string><key>EnvironmentVariables</key><dict>"
                   "<key>DYLD_INSERT_LIBRARIES</key><string>/tmp/evil.dylib</string>"
                   "</dict><key>Ignored</key><string>y")
    ARGV_PAYLOAD = ('/tmp/v</string><string>-c</string>'
                    '<string>__import__("os").system("id")</string><string>/bin/true')

    def test_an_xml_payload_in_the_working_dir_injects_no_plist_key(self):
        parsed = plistlib.loads(
            login_agent._plist_xml(spec(working_dir=self.XML_PAYLOAD)).encode())

        self.assertEqual(sorted(parsed), [
            "KeepAlive", "Label", "ProgramArguments", "RunAtLoad",
            "StandardErrorPath", "StandardOutPath", "WorkingDirectory",
        ])
        self.assertNotIn("EnvironmentVariables", parsed)
        # The payload survives as inert data in the one value it belongs to.
        self.assertEqual(parsed["WorkingDirectory"], self.XML_PAYLOAD)

    def test_an_xml_payload_in_argv_leaves_program_arguments_intact(self):
        parsed = plistlib.loads(
            login_agent._plist_xml(spec(argv=[self.ARGV_PAYLOAD, "-m", "x"])).encode())

        # Before the fix this was 8 arguments including "-c" and a system() call.
        self.assertEqual(parsed["ProgramArguments"], [self.ARGV_PAYLOAD, "-m", "x"])

    def test_a_newline_in_the_working_dir_injects_no_systemd_directive(self):
        payload = '/tmp/x\nExecStartPre=/bin/sh -c "id > /tmp/PWNED"'
        with self.assertRaisesRegex(ValueError, "newline"):
            login_agent._unit_text(spec(working_dir=payload))

    def test_a_carriage_return_is_refused_too(self):
        with self.assertRaisesRegex(ValueError, "carriage return"):
            login_agent._unit_text(spec(working_dir="/tmp/x\rExecStartPre=/bin/false"))

    def test_a_percent_specifier_is_refused_rather_than_expanded(self):
        # systemd expands %h/%t at load time, so a path containing one would name
        # something other than the value meant.
        with self.assertRaisesRegex(ValueError, "specifier"):
            login_agent._unit_text(spec(working_dir="/home/%h/testraven"))

    def test_a_payload_in_argv0_is_refused_with_the_consumers_own_wording(self):
        # program_label exists so the user is told *which* path to move.
        bad = spec(argv=["/tmp/p\nExecStartPre=/bin/false", "-m", "x"],
                   program_label="the Python executable")
        with self.assertRaisesRegex(ValueError, "Python executable"):
            login_agent._unit_text(bad)

    def test_a_payload_in_a_later_argument_is_refused_too(self):
        # Not just argv[0]: every element reaches the same ExecStart line.
        with self.assertRaisesRegex(ValueError, "argument 2"):
            login_agent._unit_text(spec(argv=["/usr/bin/python3", "-m", "x\nExecStop=/bin/false"]))

    def test_a_percent_in_the_description_is_refused(self):
        with self.assertRaisesRegex(ValueError, "specifier"):
            login_agent._unit_text(spec(description="100% local"))

    def test_unit_values_are_quoted_per_systemd_syntax(self):
        unit = login_agent._unit_text(spec(working_dir="/tmp/dir with spaces"))

        self.assertIn('WorkingDirectory="/tmp/dir with spaces"', unit)
        # Still exactly one ExecStart, and still the same command.
        self.assertEqual(len([line for line in unit.splitlines()
                             if line.startswith("ExecStart")]), 1)
        self.assertIn("-m testraven.cli serve --no-open", unit)

    def test_a_quote_in_a_path_cannot_end_the_quoted_value(self):
        unit = login_agent._unit_text(spec(working_dir='/tmp/a"b'))
        self.assertIn(r'WorkingDirectory="/tmp/a\"b"', unit)

    def test_an_argument_with_a_space_is_quoted_but_a_plain_flag_is_not(self):
        # Quoting conditionally keeps ExecStart readable while a path with a space
        # is still one word. Unquoted words are split on whitespace only, so
        # nothing else needs it.
        unit = login_agent._unit_text(spec(argv=["/usr/bin/py", "--flag", "two words"]))
        exec_line = next(line for line in unit.splitlines() if line.startswith("ExecStart"))
        self.assertEqual(exec_line, 'ExecStart="/usr/bin/py" --flag "two words"')


# ── The write path ────────────────────────────────────────────────────────────

class WriteWithBackupHardeningTests(unittest.TestCase):
    """issue #41 H3: every one of these was verified against the previous
    version of ``_write_with_backup``."""

    def test_a_symlinked_target_is_refused_rather_than_followed(self):
        # Verified before the fix: a 0600 secret was copied through the link into
        # a 0644 backup, and os.replace would then publish this content wherever
        # the link pointed.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            secret = root / "secret"
            secret.write_text("sk-ant-PLANTEDSECRET")
            os.chmod(secret, 0o600)
            (root / "unit").symlink_to(secret)

            with self.assertRaisesRegex(ValueError, "symlink"):
                login_agent._write_with_backup(root / "unit", "[Service]\n")

            self.assertEqual(secret.read_text(), "sk-ant-PLANTEDSECRET")
            self.assertEqual(list(root.glob("*-bak*")), [])

    def test_a_pre_planted_tmp_symlink_is_not_written_through(self):
        # Verified before the fix: the predictable "<name>.tmp" was written
        # *through* the planted link, and os.replace then made the unit path
        # itself an attacker-owned symlink.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            victim = root / "victim"
            victim.write_text("untouched")
            (root / "unit.tmp").symlink_to(victim)

            login_agent._write_with_backup(root / "unit", "[Service]\nnew\n")

            self.assertEqual(victim.read_text(), "untouched")
            self.assertFalse((root / "unit").is_symlink())
            self.assertEqual((root / "unit").read_text(), "[Service]\nnew\n")

    def test_the_temp_name_is_not_predictable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            names = []
            real_mkstemp = login_agent.tempfile.mkstemp

            def record(**kwargs):
                fd, name = real_mkstemp(**kwargs)
                names.append(Path(name).name)
                return fd, name

            with patch.object(login_agent.tempfile, "mkstemp", side_effect=record):
                for index in range(3):
                    login_agent._write_with_backup(root / f"unit{index}", "x")

            self.assertEqual(len(set(names)), 3, names)

    def test_a_fresh_config_file_is_not_world_readable(self):
        # Verified before the fix: 0644 at the default umask, unlike a consumer's
        # own token/state discipline.
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "unit"
            login_agent._write_with_backup(path, "[Service]\n")
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_a_backup_is_0600_before_any_content_lands(self):
        # Verified before the fix: backing up a 0600 file produced a 0644 copy.
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "unit"
            path.write_text("sk-ant-PLANTEDSECRET")
            os.chmod(path, 0o600)

            login_agent._write_with_backup(path, "new", backup_tag="tr-bak")

            backups = list(Path(tmp).glob("unit.tr-bak.*"))
            self.assertEqual(len(backups), 1)
            self.assertEqual(stat.S_IMODE(backups[0].stat().st_mode), 0o600)
            self.assertEqual(backups[0].read_text(), "sk-ant-PLANTEDSECRET")

    def test_an_existing_backup_name_is_not_silently_overwritten(self):
        # O_EXCL: a pre-planted backup name is an error, not a target.
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "unit"
            path.write_text("old")
            with patch.object(login_agent.time, "time", return_value=1000):
                (Path(tmp) / "unit.corvidae.1000").write_text("planted")
                with self.assertRaises(FileExistsError):
                    login_agent._write_with_backup(path, "new")
            self.assertEqual(path.read_text(), "old")

    def test_a_failed_write_leaves_no_temp_file_behind(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "unit"
            with patch.object(login_agent.os, "replace",
                              side_effect=OSError("read-only filesystem")):
                with self.assertRaises(OSError):
                    login_agent._write_with_backup(path, "x")
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_uninstall_refuses_to_act_on_a_symlinked_unit_path(self):
        # The unit path follows $XDG_CONFIG_HOME. Path.unlink removes the link
        # rather than its target, so this was not arbitrary deletion -- but a
        # symlink here means the installed agent was not the file we think, and
        # that is worth refusing loudly rather than quietly tidying away.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            elsewhere = root / "elsewhere"
            elsewhere.write_text("not ours")
            unit = root / "testraven.service"
            unit.symlink_to(elsewhere)

            with self.assertRaisesRegex(ValueError, "symlink"):
                _RecordingSystemd(spec(unit_path=unit)).uninstall()

            self.assertTrue(elsewhere.exists())
            self.assertTrue(unit.is_symlink())


# ── Windows ───────────────────────────────────────────────────────────────────

class WindowsStartupAgentTests(unittest.TestCase):
    def test_install_writes_a_separate_run_value_from_the_tray(self):
        # A tray owns its own value name; this must not overwrite it.
        s = spec(registry_value="TestravenDaemon", tray_registry_value="Testraven")
        winreg = _FakeWinreg()
        with redirect_stdout(io.StringIO()) as out:
            self.assertEqual(_FakeWindows(s, winreg).install(), 0)
        self.assertEqual(list(winreg.values), ["TestravenDaemon"])
        self.assertIn("testraven.cli serve --no-open", winreg.values["TestravenDaemon"])
        # The Run key starts a process once; it is not a supervisor.
        self.assertIn("does not restart it", out.getvalue())

    def test_install_defers_to_the_tray_when_it_owns_startup(self):
        # Two supervisors is the same double-owner mistake as launchd versus a
        # menu-bar app: the tray would fight a daemon the user just quit.
        s = spec(registry_value="TestravenDaemon", tray_registry_value="Testraven",
                 tray_owner="Windows tray app")
        winreg = _FakeWinreg({"Testraven": r'"C:\TR\TR.exe"'})
        with redirect_stderr(io.StringIO()) as err:
            self.assertEqual(_FakeWindows(s, winreg).install(), 1)
        self.assertNotIn("TestravenDaemon", winreg.values)
        self.assertIn("Windows tray app already starts testraven at login", err.getvalue())

    def test_a_consumer_with_no_tray_never_looks_for_one(self):
        # An empty tray_registry_value means "we ship no tray". Inventing a name
        # would let an unrelated key's presence refuse a valid install.
        winreg = _FakeWinreg({"Testraven": "someone else's app"})
        agent = _FakeWindows(spec(tray_registry_value=""), winreg)
        self.assertFalse(agent.tray_owns_startup())
        with redirect_stdout(io.StringIO()):
            self.assertEqual(agent.install(), 0)

    def test_install_prefers_pythonw_to_avoid_a_console_window(self):
        # python.exe would flash a console window at every login, which a tray
        # shell already goes out of its way to avoid.
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "pythonw.exe").write_text("")
            command = login_agent._run_command(
                spec(argv=[str(Path(tmp) / "python.exe"), "-m", "testraven.cli"]))
        self.assertIn(str(Path(tmp) / "pythonw.exe"), command)
        self.assertIn("-m testraven.cli", command)

    def test_command_falls_back_to_the_given_executable(self):
        with tempfile.TemporaryDirectory() as tmp:
            command = login_agent._run_command(
                spec(argv=[str(Path(tmp) / "python.exe"), "-m", "x"]))
        self.assertIn("python.exe", command)
        self.assertNotIn("pythonw", command)

    def test_a_single_word_argv_is_still_quoted(self):
        # The Run key takes a command line, so a space in the path would otherwise
        # be read as an argument boundary.
        self.assertEqual(login_agent._run_command(spec(argv=["/a b/app"])), '"/a b/app"')

    def test_installed_reports_only_the_daemon_value(self):
        s = spec(registry_value="TestravenDaemon", tray_registry_value="Testraven")
        tray_only = _FakeWindows(s, _FakeWinreg({"Testraven": "tray"}))
        self.assertFalse(tray_only.installed())
        self.assertTrue(tray_only.tray_owns_startup())

        daemon_only = _FakeWindows(s, _FakeWinreg({"TestravenDaemon": "daemon"}))
        self.assertTrue(daemon_only.installed())
        self.assertFalse(daemon_only.tray_owns_startup())

    def test_missing_run_key_is_not_an_error(self):
        self.assertFalse(_FakeWindows(spec(), _FakeWinreg(key_exists=False)).installed())

    def test_uninstall_removes_only_the_daemon_value(self):
        s = spec(registry_value="TestravenDaemon", tray_registry_value="Testraven")
        winreg = _FakeWinreg({"TestravenDaemon": "daemon", "Testraven": "tray"})
        with redirect_stdout(io.StringIO()) as out:
            self.assertEqual(_FakeWindows(s, winreg).uninstall(), 0)
        self.assertEqual(list(winreg.values), ["Testraven"])
        self.assertIn("removed", out.getvalue())

    def test_uninstall_is_a_no_op_when_absent(self):
        with redirect_stdout(io.StringIO()) as out:
            self.assertEqual(_FakeWindows(spec(), _FakeWinreg()).uninstall(), 0)
        self.assertIn("not installed", out.getvalue())


# ── Selection ─────────────────────────────────────────────────────────────────

class AgentSelectionTests(unittest.TestCase):
    def test_each_supported_platform_selects_its_own_backend(self):
        s = spec()
        self.assertIsInstance(get_login_agent(s, "darwin"), LaunchdAgent)
        self.assertIsInstance(get_login_agent(s, "linux"), SystemdUserAgent)
        self.assertIsInstance(get_login_agent(s, "win32"), WindowsStartupAgent)
        self.assertIsInstance(get_login_agent(s, "cygwin"), WindowsStartupAgent)

    def test_unknown_platform_has_no_backend(self):
        # None rather than an exception: "no start-at-login mechanism I know" is a
        # thing to report with an exit code, not to unwind through a CLI.
        self.assertIsNone(get_login_agent(spec(), "freebsd14"))

    def test_the_selected_backend_carries_the_spec_it_was_given(self):
        s = spec()
        self.assertIs(get_login_agent(s, "darwin").spec, s)


if __name__ == "__main__":
    unittest.main()
