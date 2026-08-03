"""Per-platform start-at-login backends, exercised from any host -- issue #39.

Every backend is driven through the ``LoginAgent`` seam with its own
subprocess/registry boundary patched, so launchd, systemd, and Windows startup
are all covered on the macOS dev machine without requiring those OSes.

The implementation moved to ``corvidae.login_agent`` in issue #42, and this file
deliberately still drives it through ``huginn.agent_install``: what it covers is
that *Huginn's* login agents behave as they did, spec and all. Only the two
assertions that patch a stdlib module the implementation uses (``time``,
``tempfile``) reach into ``corvidae.login_agent``, because that is where the code
under test now lives. corvidae has its own tests for the generic behaviour.
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

from huginn import agent_install
from huginn.agent_install import (
    LaunchdAgent,
    SystemdUserAgent,
    WindowsStartupAgent,
    get_login_agent,
)


def _ok(stdout: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], 0, stdout, "")


def _fail(stderr: str = "boom") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], 1, "", stderr)


class AgentSelectionTests(unittest.TestCase):
    def test_each_supported_platform_selects_its_own_backend(self):
        self.assertIsInstance(get_login_agent("darwin"), LaunchdAgent)
        self.assertIsInstance(get_login_agent("linux"), SystemdUserAgent)
        self.assertIsInstance(get_login_agent("win32"), WindowsStartupAgent)
        self.assertIsInstance(get_login_agent("cygwin"), WindowsStartupAgent)

    def test_unknown_platform_has_no_backend(self):
        self.assertIsNone(get_login_agent("freebsd14"))

    def test_install_and_uninstall_refuse_an_unsupported_platform(self):
        with (patch.object(agent_install.sys, "platform", "freebsd14"),
              redirect_stderr(io.StringIO()) as stderr):
            self.assertEqual(agent_install.install(), 2)
            self.assertEqual(agent_install.uninstall(), 2)
        self.assertIn("not supported on freebsd14", stderr.getvalue())


class LaunchdAgentTests(unittest.TestCase):
    """macOS behaviour, including the deliberate KeepAlive conflict."""

    def test_keepalive_stays_in_the_plist(self):
        # launchd's KeepAlive resurrects a daemon the user quit. That used to
        # conflict with Huginn.app owning the lifecycle; with that app deleted it
        # conflicts with the menu bar's Quit row instead, which is the same
        # trade-off and is documented the same way (uninstall-agent first). The
        # incompatibility must stay real rather than be papered over by dropping
        # KeepAlive, which would quietly change the restart policy for every user
        # who installed the agent for it.
        xml = agent_install._plist_xml()
        self.assertIn("<key>KeepAlive</key>", xml)
        self.assertIn("<key>RunAtLoad</key>", xml)
        self.assertIn("huginn.cli", xml)
        self.assertIn("--no-open", xml)

    def test_install_backs_up_an_existing_plist_and_loads_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            plist = Path(tmp) / "is.tohuw.huginn.plist"
            plist.write_text("<plist>old</plist>")
            calls = []

            def launchctl(*args):
                calls.append(args)
                return _fail() if args[0] == "list" else _ok()

            with (patch.object(agent_install, "PLIST_PATH", plist),
                  patch.object(agent_install, "LOG_PATH", Path(tmp) / "agent.log"),
                  patch.object(agent_install, "_launchctl", side_effect=launchctl),
                  redirect_stdout(io.StringIO()) as out):
                self.assertEqual(LaunchdAgent().install(), 0)

            self.assertIn("<key>KeepAlive</key>", plist.read_text())
            backups = list(Path(tmp).glob("is.tohuw.huginn.plist.huginn-bak.*"))
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_text(), "<plist>old</plist>")
            self.assertEqual(calls[-1], ("load", "-w", str(plist)))
            self.assertFalse(list(Path(tmp).glob("*.tmp")))
            self.assertIn("start at login", out.getvalue())

    def test_install_unloads_a_currently_loaded_agent_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            plist = Path(tmp) / "is.tohuw.huginn.plist"
            calls = []

            with (patch.object(agent_install, "PLIST_PATH", plist),
                  patch.object(agent_install, "LOG_PATH", Path(tmp) / "agent.log"),
                  patch.object(agent_install, "_launchctl",
                               side_effect=lambda *a: calls.append(a) or _ok()),
                  redirect_stdout(io.StringIO())):
                self.assertEqual(LaunchdAgent().install(), 0)

            self.assertEqual(calls[0], ("list", agent_install.LABEL))
            self.assertEqual(calls[1], ("unload", "-w", str(plist)))

    def test_install_reports_a_failed_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            plist = Path(tmp) / "is.tohuw.huginn.plist"
            with (patch.object(agent_install, "PLIST_PATH", plist),
                  patch.object(agent_install, "LOG_PATH", Path(tmp) / "agent.log"),
                  patch.object(agent_install, "_launchctl",
                               side_effect=lambda *a: _fail() if a[0] != "list" else _fail()),
                  redirect_stdout(io.StringIO()) as out):
                self.assertEqual(LaunchdAgent().install(), 1)
            self.assertIn("launchctl load failed", out.getvalue())

    def test_uninstall_unloads_and_removes_the_plist(self):
        with tempfile.TemporaryDirectory() as tmp:
            plist = Path(tmp) / "is.tohuw.huginn.plist"
            plist.write_text("<plist/>")
            with (patch.object(agent_install, "PLIST_PATH", plist),
                  patch.object(agent_install, "_launchctl", return_value=_ok()) as launchctl,
                  redirect_stdout(io.StringIO()) as out):
                self.assertEqual(LaunchdAgent().uninstall(), 0)
            self.assertFalse(plist.exists())
            launchctl.assert_called_once_with("unload", "-w", str(plist))
            self.assertIn("removed", out.getvalue())

    def test_uninstall_is_a_no_op_when_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            with (patch.object(agent_install, "PLIST_PATH", Path(tmp) / "none.plist"),
                  patch.object(agent_install, "_launchctl") as launchctl,
                  redirect_stdout(io.StringIO()) as out):
                self.assertEqual(LaunchdAgent().uninstall(), 0)
            launchctl.assert_not_called()
            self.assertIn("not installed", out.getvalue())

    def test_launchctl_itself_is_guarded_off_macos(self):
        with patch.object(agent_install.sys, "platform", "win32"):
            with self.assertRaisesRegex(RuntimeError, "only available on macOS"):
                agent_install._launchctl("list")


class SystemdUserAgentTests(unittest.TestCase):
    """Linux behaviour, verified on the macOS dev host through the seam."""

    def test_unit_restarts_on_failure_but_honours_a_deliberate_stop(self):
        # Not KeepAlive's semantics on purpose: `systemctl --user stop huginn`
        # must stay effective.
        unit = agent_install._unit_text()
        self.assertIn("Restart=on-failure", unit)
        self.assertNotIn("Restart=always", unit)
        self.assertIn("WantedBy=default.target", unit)
        self.assertIn("-m huginn.cli serve --no-open", unit)

    def test_install_writes_the_unit_then_reloads_and_enables_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            unit = Path(tmp) / "systemd" / "user" / "huginn.service"
            calls = []
            with (patch.object(agent_install, "UNIT_PATH", unit),
                  patch.object(agent_install, "_systemctl",
                               side_effect=lambda *a: calls.append(a) or _ok()),
                  redirect_stdout(io.StringIO()) as out):
                self.assertEqual(SystemdUserAgent().install(), 0)

            self.assertIn("Restart=on-failure", unit.read_text())
            self.assertEqual(calls, [("daemon-reload",), ("enable", "--now", "huginn.service")])
            # A user unit stops at logout without lingering -- surface it.
            self.assertIn("enable-linger", out.getvalue())

    def test_install_backs_up_an_existing_unit(self):
        with tempfile.TemporaryDirectory() as tmp:
            unit = Path(tmp) / "huginn.service"
            unit.write_text("[Service]\nExecStart=/old\n")
            with (patch.object(agent_install, "UNIT_PATH", unit),
                  patch.object(agent_install, "_systemctl", return_value=_ok()),
                  redirect_stdout(io.StringIO())):
                self.assertEqual(SystemdUserAgent().install(), 0)
            backups = list(Path(tmp).glob("huginn.service.huginn-bak.*"))
            self.assertEqual(len(backups), 1)
            self.assertIn("ExecStart=/old", backups[0].read_text())
            self.assertFalse(list(Path(tmp).glob("*.tmp")))

    def test_install_reports_a_failed_enable(self):
        with tempfile.TemporaryDirectory() as tmp:
            unit = Path(tmp) / "huginn.service"
            with (patch.object(agent_install, "UNIT_PATH", unit),
                  patch.object(agent_install, "_systemctl",
                               side_effect=[_ok(), _fail("unit is masked")]),
                  redirect_stdout(io.StringIO()) as out):
                self.assertEqual(SystemdUserAgent().install(), 1)
            self.assertIn("systemctl enable failed", out.getvalue())
            self.assertIn("unit is masked", out.getvalue())

    def test_install_reports_a_failed_daemon_reload(self):
        with tempfile.TemporaryDirectory() as tmp:
            unit = Path(tmp) / "huginn.service"
            with (patch.object(agent_install, "UNIT_PATH", unit),
                  patch.object(agent_install, "_systemctl", side_effect=[_fail("no dbus")]),
                  redirect_stdout(io.StringIO()) as out):
                self.assertEqual(SystemdUserAgent().install(), 1)
            self.assertIn("daemon-reload failed", out.getvalue())

    def test_uninstall_disables_removes_and_reloads(self):
        with tempfile.TemporaryDirectory() as tmp:
            unit = Path(tmp) / "huginn.service"
            unit.write_text("[Service]\n")
            calls = []
            with (patch.object(agent_install, "UNIT_PATH", unit),
                  patch.object(agent_install, "_systemctl",
                               side_effect=lambda *a: calls.append(a) or _ok()),
                  redirect_stdout(io.StringIO()) as out):
                self.assertEqual(SystemdUserAgent().uninstall(), 0)
            self.assertFalse(unit.exists())
            self.assertEqual(calls, [("disable", "--now", "huginn.service"),
                                     ("daemon-reload",)])
            self.assertIn("removed", out.getvalue())

    def test_uninstall_is_a_no_op_when_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            with (patch.object(agent_install, "UNIT_PATH", Path(tmp) / "huginn.service"),
                  patch.object(agent_install, "_systemctl") as systemctl,
                  redirect_stdout(io.StringIO()) as out):
                self.assertEqual(SystemdUserAgent().uninstall(), 0)
            systemctl.assert_not_called()
            self.assertIn("not installed", out.getvalue())

    def test_unit_path_follows_xdg_config_home(self):
        self.assertTrue(str(agent_install.UNIT_PATH).endswith("systemd/user/huginn.service"))

    def test_systemctl_itself_is_guarded_off_linux(self):
        with patch.object(agent_install.sys, "platform", "darwin"):
            with self.assertRaisesRegex(RuntimeError, "only available on Linux"):
                agent_install._systemctl("daemon-reload")


class ConfigInjectionTests(unittest.TestCase):
    """issue #41 C3: ``_PLIST_TEMPLATE``/``_UNIT_TEMPLATE`` were ``str.format``
    into structured config with zero escaping, and ``{cwd}`` is ``REPO_ROOT`` --
    a filesystem path -- while ``{python}`` is ``sys.executable``. A directory
    name containing XML (macOS) or a newline (Linux) injected arbitrary
    directives into *persistent auto-start config* that ``KeepAlive`` then
    relaunches forever. A payload erroring out is not a mitigation: the ones
    below parsed cleanly before the fix."""

    # Closes WorkingDirectory's <string>, adds a whole key/value pair, reopens a
    # string so the document stays balanced. Verified to parse cleanly and yield
    # a live DYLD_INSERT_LIBRARIES against the pre-fix template.
    XML_PAYLOAD = ("/tmp/x</string><key>EnvironmentVariables</key><dict>"
                   "<key>DYLD_INSERT_LIBRARIES</key><string>/tmp/evil.dylib</string>"
                   "</dict><key>Ignored</key><string>y")
    ARGV_PAYLOAD = ('/tmp/v</string><string>-c</string>'
                    '<string>__import__("os").system("id")</string><string>/bin/true')

    def test_an_xml_payload_in_repo_root_injects_no_plist_key(self):
        with patch.object(agent_install, "REPO_ROOT", self.XML_PAYLOAD):
            parsed = plistlib.loads(agent_install._plist_xml().encode())

        self.assertEqual(sorted(parsed), [
            "KeepAlive", "Label", "ProgramArguments", "RunAtLoad",
            "StandardErrorPath", "StandardOutPath", "WorkingDirectory",
        ])
        self.assertNotIn("EnvironmentVariables", parsed)
        # The payload survives as inert data in the one value it belongs to.
        self.assertEqual(parsed["WorkingDirectory"], self.XML_PAYLOAD)

    def test_an_xml_payload_in_sys_executable_leaves_program_arguments_intact(self):
        with patch.object(agent_install.sys, "executable", self.ARGV_PAYLOAD):
            parsed = plistlib.loads(agent_install._plist_xml().encode())

        # Before the fix this was 8 arguments including "-c" and a system() call.
        self.assertEqual(parsed["ProgramArguments"],
                         [self.ARGV_PAYLOAD, "-m", "huginn.cli", "serve", "--no-open"])

    def test_the_plist_is_still_the_agent_it_was(self):
        parsed = plistlib.loads(agent_install._plist_xml().encode())

        self.assertIs(parsed["KeepAlive"], True)
        self.assertIs(parsed["RunAtLoad"], True)
        self.assertEqual(parsed["Label"], agent_install.LABEL)
        self.assertEqual(parsed["ProgramArguments"][1:],
                         ["-m", "huginn.cli", "serve", "--no-open"])

    def test_a_newline_in_repo_root_injects_no_systemd_directive(self):
        payload = '/tmp/x\nExecStartPre=/bin/sh -c "id > /tmp/PWNED"'

        with patch.object(agent_install, "REPO_ROOT", payload):
            with self.assertRaisesRegex(ValueError, "newline"):
                agent_install._unit_text()

    def test_a_carriage_return_is_refused_too(self):
        with patch.object(agent_install, "REPO_ROOT", "/tmp/x\rExecStartPre=/bin/false"):
            with self.assertRaisesRegex(ValueError, "carriage return"):
                agent_install._unit_text()

    def test_a_percent_specifier_is_refused_rather_than_expanded(self):
        # systemd expands %h/%t at load time, so a path containing one would
        # name something other than the checkout meant.
        with patch.object(agent_install, "REPO_ROOT", "/home/%h/huginn"):
            with self.assertRaisesRegex(ValueError, "specifier"):
                agent_install._unit_text()

    def test_a_payload_in_sys_executable_is_refused_for_systemd_too(self):
        with patch.object(agent_install.sys, "executable", "/tmp/p\nExecStartPre=/bin/false"):
            with self.assertRaisesRegex(ValueError, "Python executable"):
                agent_install._unit_text()

    def test_unit_values_are_quoted_per_systemd_syntax(self):
        with patch.object(agent_install, "REPO_ROOT", "/tmp/dir with spaces"):
            unit = agent_install._unit_text()

        self.assertIn('WorkingDirectory="/tmp/dir with spaces"', unit)
        # Still exactly one ExecStart, and still the same command.
        self.assertEqual(len([line for line in unit.splitlines()
                             if line.startswith("ExecStart")]), 1)
        self.assertIn("-m huginn.cli serve --no-open", unit)

    def test_a_quote_in_a_path_cannot_end_the_quoted_value(self):
        with patch.object(agent_install, "REPO_ROOT", '/tmp/a"b'):
            unit = agent_install._unit_text()

        self.assertIn(r'WorkingDirectory="/tmp/a\"b"', unit)


class WriteWithBackupHardeningTests(unittest.TestCase):
    """issue #41 H3: every one of these was verified against the previous
    version of ``_write_with_backup``."""

    def test_a_symlinked_target_is_refused_rather_than_followed(self):
        # Verified before the fix: a 0600 secret was copied through the link
        # into a 0644 .huginn-bak.<ts> file, and os.replace would then publish
        # this content wherever the link pointed.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            secret = root / "secret"
            secret.write_text("sk-ant-PLANTEDSECRET")
            os.chmod(secret, 0o600)
            (root / "huginn.service").symlink_to(secret)

            with self.assertRaisesRegex(ValueError, "symlink"):
                agent_install._write_with_backup(root / "huginn.service", "[Service]\n")

            self.assertEqual(secret.read_text(), "sk-ant-PLANTEDSECRET")
            self.assertEqual(list(root.glob("*huginn-bak*")), [])

    def test_a_pre_planted_tmp_symlink_is_not_written_through(self):
        # Verified before the fix: the predictable "<name>.tmp" was written
        # *through* the planted link, and os.replace then made the unit path
        # itself an attacker-owned symlink.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            victim = root / "victim"
            victim.write_text("untouched")
            (root / "huginn.service.tmp").symlink_to(victim)

            agent_install._write_with_backup(root / "huginn.service", "[Service]\nnew\n")

            self.assertEqual(victim.read_text(), "untouched")
            unit = root / "huginn.service"
            self.assertFalse(unit.is_symlink())
            self.assertEqual(unit.read_text(), "[Service]\nnew\n")

    def test_the_temp_name_is_not_predictable(self):
        # Patched at corvidae's module rather than huginn's since issue #42 moved
        # the implementation there. Still driven through
        # ``agent_install._write_with_backup``, because Huginn's entry point and
        # its ``.huginn-bak.`` tag are what this file is here to cover.
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
                    agent_install._write_with_backup(root / f"unit{index}", "x")

            self.assertEqual(len(set(names)), 3, names)

    def test_a_fresh_config_file_is_not_world_readable(self):
        # Verified before the fix: 0644 at the default umask, unlike this
        # codebase's own config.secure_dir/write_token/_write_snapshot.
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "huginn.service"
            agent_install._write_with_backup(path, "[Service]\n")
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_a_backup_is_0600_before_any_content_lands(self):
        # Verified before the fix: backing up a 0600 file produced a 0644 copy.
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "huginn.service"
            path.write_text("sk-ant-PLANTEDSECRET")
            os.chmod(path, 0o600)

            agent_install._write_with_backup(path, "[Service]\nnew\n")

            backups = list(Path(tmp).glob("huginn.service.huginn-bak.*"))
            self.assertEqual(len(backups), 1)
            self.assertEqual(stat.S_IMODE(backups[0].stat().st_mode), 0o600)
            self.assertEqual(backups[0].read_text(), "sk-ant-PLANTEDSECRET")

    def test_an_existing_backup_name_is_not_silently_overwritten(self):
        # O_EXCL: a pre-planted backup name is an error, not a target.
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "huginn.service"
            path.write_text("old")
            with patch.object(login_agent.time, "time", return_value=1000):
                (Path(tmp) / "huginn.service.huginn-bak.1000").write_text("planted")
                with self.assertRaises(FileExistsError):
                    agent_install._write_with_backup(path, "new")
            self.assertEqual(path.read_text(), "old")

    def test_a_failed_write_leaves_no_temp_file_behind(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "huginn.service"
            with patch.object(agent_install.os, "replace",
                              side_effect=OSError("read-only filesystem")):
                with self.assertRaises(OSError):
                    agent_install._write_with_backup(path, "x")
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_uninstall_refuses_to_act_on_a_symlinked_unit_path(self):
        # UNIT_PATH follows $XDG_CONFIG_HOME. Path.unlink removes the link
        # rather than its target, so this was not arbitrary deletion -- but a
        # symlink here means the installed agent was not the file we think, and
        # that is worth refusing loudly rather than quietly tidying away.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            elsewhere = root / "elsewhere"
            elsewhere.write_text("not ours")
            unit = root / "huginn.service"
            unit.symlink_to(elsewhere)

            with (patch.object(agent_install, "UNIT_PATH", unit),
                  patch.object(agent_install, "_systemctl", return_value=_ok())):
                with self.assertRaisesRegex(ValueError, "symlink"):
                    SystemdUserAgent().uninstall()

            self.assertTrue(elsewhere.exists())
            self.assertTrue(unit.is_symlink())


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


class WindowsStartupAgentTests(unittest.TestCase):
    """Windows behaviour, verified on the macOS dev host through the seam."""

    def test_install_writes_a_separate_run_value_from_the_tray(self):
        # The deleted windows tray owned "Huginn". The CLI must still not
        # overwrite it: the code is gone, but the registry value it wrote is
        # still on the machines that ran it.
        winreg = _FakeWinreg()
        with (patch.object(agent_install, "_winreg", return_value=winreg),
              redirect_stdout(io.StringIO()) as out):
            self.assertEqual(WindowsStartupAgent().install(), 0)
        self.assertIn(agent_install.DAEMON_RUN_VALUE, winreg.values)
        self.assertNotIn(agent_install.TRAY_RUN_VALUE, winreg.values)
        self.assertIn("huginn.cli serve --no-open",
                      winreg.values[agent_install.DAEMON_RUN_VALUE])
        # The Run key starts a process once; it is not a supervisor.
        self.assertIn("does not restart it", out.getvalue())

    def test_install_defers_to_a_leftover_tray_autostart(self):
        # Two autostarts would resurrect a daemon the user just quit. This is the
        # test that matters *more* now the tray is deleted, not less: an upgraded
        # machine has the stale "Huginn" value and no tray to explain it, so the
        # refusal is the only thing that tells the user what is going on.
        winreg = _FakeWinreg({agent_install.TRAY_RUN_VALUE: r'"C:\Huginn\Huginn.exe"'})
        with (patch.object(agent_install, "_winreg", return_value=winreg),
              redirect_stderr(io.StringIO()) as stderr):
            self.assertEqual(WindowsStartupAgent().install(), 1)
        self.assertNotIn(agent_install.DAEMON_RUN_VALUE, winreg.values)
        self.assertIn("already starts huginn at login", stderr.getvalue())

    def test_install_prefers_pythonw_to_avoid_a_console_window(self):
        # python.exe would flash a console window at every login. Still the right
        # behaviour with the tray gone: this is the only Windows autostart now, so
        # it is the only thing standing between a login and a stray console.
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "pythonw.exe").write_text("")
            with patch.object(agent_install.sys, "executable", str(Path(tmp) / "python.exe")):
                command = agent_install._daemon_command()
        self.assertIn(str(Path(tmp) / "pythonw.exe"), command)
        self.assertIn("-m huginn.cli serve --no-open", command)

    def test_command_falls_back_to_the_running_interpreter(self):
        with tempfile.TemporaryDirectory() as tmp:
            executable = Path(tmp) / "python.exe"
            with patch.object(agent_install.sys, "executable", str(executable)):
                command = agent_install._daemon_command()
        self.assertIn("python.exe", command)
        self.assertNotIn("pythonw", command)

    def test_installed_reports_only_the_daemon_value(self):
        agent = WindowsStartupAgent()
        tray_only = _FakeWinreg({agent_install.TRAY_RUN_VALUE: "tray"})
        with patch.object(agent_install, "_winreg", return_value=tray_only):
            self.assertFalse(agent.installed())
            self.assertTrue(agent.tray_owns_startup())

        both = _FakeWinreg({agent_install.DAEMON_RUN_VALUE: "daemon"})
        with patch.object(agent_install, "_winreg", return_value=both):
            self.assertTrue(agent.installed())
            self.assertFalse(agent.tray_owns_startup())

    def test_missing_run_key_is_not_an_error(self):
        with patch.object(agent_install, "_winreg",
                          return_value=_FakeWinreg(key_exists=False)):
            self.assertFalse(WindowsStartupAgent().installed())

    def test_uninstall_removes_only_the_daemon_value(self):
        winreg = _FakeWinreg({
            agent_install.DAEMON_RUN_VALUE: "daemon",
            agent_install.TRAY_RUN_VALUE: "tray",
        })
        with (patch.object(agent_install, "_winreg", return_value=winreg),
              redirect_stdout(io.StringIO()) as out):
            self.assertEqual(WindowsStartupAgent().uninstall(), 0)
        self.assertEqual(list(winreg.values), [agent_install.TRAY_RUN_VALUE])
        self.assertIn("removed", out.getvalue())

    def test_uninstall_is_a_no_op_when_absent(self):
        winreg = _FakeWinreg()
        with (patch.object(agent_install, "_winreg", return_value=winreg),
              redirect_stdout(io.StringIO()) as out):
            self.assertEqual(WindowsStartupAgent().uninstall(), 0)
        self.assertIn("not installed", out.getvalue())


class UninstallEveryPlatformTests(unittest.TestCase):
    """`huginn uninstall-agent` has to work everywhere install-agent does."""

    def test_uninstall_dispatches_to_each_platform_backend(self):
        for platform, agent_class in (("darwin", LaunchdAgent),
                                      ("linux", SystemdUserAgent),
                                      ("win32", WindowsStartupAgent)):
            with self.subTest(platform=platform):
                with (patch.object(agent_install.sys, "platform", platform),
                      patch.object(agent_class, "uninstall", return_value=0) as uninstall):
                    self.assertEqual(agent_install.uninstall(), 0)
                uninstall.assert_called_once_with()

    def test_install_dispatches_to_each_platform_backend(self):
        for platform, agent_class in (("darwin", LaunchdAgent),
                                      ("linux", SystemdUserAgent),
                                      ("win32", WindowsStartupAgent)):
            with self.subTest(platform=platform):
                with (patch.object(agent_install.sys, "platform", platform),
                      patch.object(agent_class, "install", return_value=0) as install):
                    self.assertEqual(agent_install.install(), 0)
                install.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
