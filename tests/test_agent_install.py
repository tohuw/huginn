"""Per-platform start-at-login backends, exercised from any host -- issue #39.

Every backend is driven through the ``LoginAgent`` seam with its own
subprocess/registry boundary patched, so launchd, systemd, and Windows startup
are all covered on the macOS dev machine without requiring those OSes.
"""
from __future__ import annotations

import io
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

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
        # Huginn.app owns the daemon lifecycle; launchd's KeepAlive would
        # resurrect a daemon the user quit. README documents removing this
        # agent first, so the incompatibility must remain real, not silently
        # papered over by dropping KeepAlive.
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
        # windows/Huginn.Tray owns "Huginn"; the CLI must not overwrite it.
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

    def test_install_defers_to_the_tray_when_it_owns_startup(self):
        # Two supervisors is the same double-owner mistake as launchd vs
        # Huginn.app: the tray would fight a daemon the user just quit.
        winreg = _FakeWinreg({agent_install.TRAY_RUN_VALUE: r'"C:\Huginn\Huginn.exe"'})
        with (patch.object(agent_install, "_winreg", return_value=winreg),
              redirect_stderr(io.StringIO()) as stderr):
            self.assertEqual(WindowsStartupAgent().install(), 1)
        self.assertNotIn(agent_install.DAEMON_RUN_VALUE, winreg.values)
        self.assertIn("tray app already starts huginn at login", stderr.getvalue())

    def test_install_prefers_pythonw_to_avoid_a_console_window(self):
        # python.exe would flash a console window at every login, which the
        # tray shell already goes out of its way to avoid.
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
