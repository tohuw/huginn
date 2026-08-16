"""Focusing an exact tab, using coordinates the terminal issued.

A pid names a process, not a place on screen, and on Windows the gap is total:
Windows Terminal runs every window and every tab in one process behind one
HWND, so every session on the machine resolves to the same window and a jump
lands on whichever tab was already showing. Nothing in its UI Automation tree
separates them -- every element reports the same ProcessId and the tab items
carry no AutomationId, only the title the shell set.

Terminals that *can* answer do it through the environment of the processes they
host. WezTerm exports WEZTERM_PANE, its control socket and its own executable
into every pane; Huginn's hook is the one component running inside the session,
so it is the one that can read them. Verified end to end against a live
WezTerm before this was written: focus_session drove the window's active-tab
indicator to [1/3], [2/3] and [3/3] on demand.
"""
from __future__ import annotations

import os
import subprocess
import unittest
from unittest.mock import patch

from huginn.focus import focus_session
from huginn.hooks.cli import _terminal_identity
from huginn.model import Session, SessionState
from huginn.platform.windows import WindowsPlatform

WEZ = {"kind": "wezterm", "pane": "7",
       "socket": r"C:\Users\me\.local\share\wezterm\gui-sock-42284",
       "executable": r"C:\Program Files\WezTerm\wezterm.exe"}


def claude_session(**kw) -> Session:
    base = dict(key="claude:1", source="claude", session_id="s", cwd=r"C:\repo",
                name="probe", pid=1, entrypoint="cli",
                state=SessionState.WORKING, state_since=0.0)
    base.update(kw)
    return Session(**base)


class HookCapturesTerminalIdentity(unittest.TestCase):
    """The hook is the only part of Huginn inside the session's terminal."""

    def _identity(self, env: dict[str, str]):
        with patch.dict(os.environ, env, clear=True):
            return _terminal_identity()

    def test_a_wezterm_pane_reports_everything_needed(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            # A real file, because the resolver verifies the binary exists
            # rather than recording a path that might not run.
            cli = os.path.join(tmp, "wezterm.exe" if os.name == "nt" else "wezterm")
            open(cli, "wb").close()
            identity = self._identity({
                "WEZTERM_PANE": "7",
                "WEZTERM_UNIX_SOCKET": "/tmp/gui-sock-1",
                "WEZTERM_EXECUTABLE": cli,
                "WEZTERM_EXECUTABLE_DIR": tmp,
            })
        self.assertEqual(identity, {"kind": "wezterm", "pane": "7",
                                    "socket": "/tmp/gui-sock-1",
                                    "executable": cli})

    def test_the_socket_is_recorded_because_the_daemon_cannot_find_it(self):
        """The daemon runs outside every pane; only the pane knows its socket."""
        self.assertIn("socket", self._identity({
            "WEZTERM_PANE": "7", "WEZTERM_UNIX_SOCKET": "/tmp/gui-sock-1"}))

    def test_a_pane_id_alone_is_enough(self):
        self.assertEqual(self._identity({"WEZTERM_PANE": "3"}),
                         {"kind": "wezterm", "pane": "3"})

    def test_it_records_the_binary_that_speaks_cli_not_the_one_running_us(self):
        """WEZTERM_EXECUTABLE is wezterm-gui.exe, which rejects `cli`.

        Caught in production. Recording it verbatim produced an identity that
        looked complete and failed at the only moment it was needed:
        `wezterm-gui.exe cli list` exits 2 with "unrecognized subcommand".
        """
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            gui = os.path.join(tmp, "wezterm-gui.exe")
            cli = os.path.join(tmp, "wezterm.exe")
            for path in (gui, cli):
                open(path, "wb").close()
            identity = self._identity({
                "WEZTERM_PANE": "1",
                "WEZTERM_EXECUTABLE": gui,
                "WEZTERM_EXECUTABLE_DIR": tmp,
            })
        self.assertEqual(identity["executable"], cli)
        self.assertNotIn("-gui", identity["executable"])

    def test_a_gui_binary_with_no_sibling_cli_is_not_recorded(self):
        """Better no executable -- and a PATH lookup -- than a wrong one."""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            gui = os.path.join(tmp, "wezterm-gui.exe")
            open(gui, "wb").close()
            identity = self._identity({"WEZTERM_PANE": "1", "WEZTERM_EXECUTABLE": gui})
        self.assertNotIn("executable", identity)

    def test_windows_terminal_records_nothing(self):
        """WT_SESSION identifies a pane but cannot be used to focus one.

        Windows Terminal exports a per-pane GUID, so a process *can* learn where
        it is -- and there is no API to act on it. Identity without addressing
        is not worth recording, and recording it would imply a route that does
        not exist.
        """
        self.assertIsNone(self._identity({
            "WT_SESSION": "bf3081be-b0e2-456f-bc97-e277d5c9acbf",
            "WT_PROFILE_ID": "{574e775e-4f2a-5b96-ac1e-a2962a402336}"}))

    def test_no_terminal_reports_nothing(self):
        self.assertIsNone(self._identity({}))


class ActivatingThePane(unittest.TestCase):
    def _run(self, returncode=0, stderr=""):
        return patch.object(
            subprocess, "run",
            return_value=subprocess.CompletedProcess([], returncode, "", stderr))

    def test_it_addresses_the_pane_by_id(self):
        with self._run() as run:
            result = WindowsPlatform().focus_pane(WEZ)
        argv = run.call_args.args[0]
        self.assertEqual(argv[:3], [WEZ["executable"], "cli", "activate-pane"])
        self.assertEqual(argv[3:], ["--pane-id", "7"])
        self.assertTrue(result.ok)
        self.assertEqual(result.target, "WezTerm")

    def test_the_recorded_socket_reaches_the_subprocess(self):
        """Without it the CLI cannot find the GUI and exits non-zero."""
        with self._run() as run:
            WindowsPlatform().focus_pane(WEZ)
        self.assertEqual(run.call_args.kwargs["env"]["WEZTERM_UNIX_SOCKET"],
                         WEZ["socket"])

    def test_a_refusal_is_reported_not_raised(self):
        with self._run(returncode=1, stderr="no such pane: 7"):
            result = WindowsPlatform().focus_pane(WEZ)
        self.assertFalse(result.ok)
        self.assertIn("no such pane", result.detail)

    def test_a_missing_binary_is_reported_not_raised(self):
        with patch.object(subprocess, "run", side_effect=FileNotFoundError):
            result = WindowsPlatform().focus_pane(WEZ)
        self.assertFalse(result.ok)
        self.assertIn("unavailable", result.detail)

    def test_an_unknown_terminal_is_refused(self):
        result = WindowsPlatform().focus_pane({"kind": "someterm", "pane": "1"})
        self.assertFalse(result.ok)

    def test_the_pipe_is_decoded_as_utf8_not_the_locale(self):
        """WezTerm echoes tab titles, and those are full of non-cp1252 text.

        Caught in production: the first version used text=True, which decodes
        with the *locale* encoding, and a tab titled "◐ Set up Huginn…" raised
        UnicodeDecodeError. That is neither OSError nor SubprocessError, so it
        escaped the handler below and would have taken the focus request with
        it. Third time this class of bug appeared today.
        """
        with self._run() as run:
            WindowsPlatform().focus_pane(WEZ)
        self.assertEqual(run.call_args.kwargs.get("encoding"), "utf-8")
        self.assertEqual(run.call_args.kwargs.get("errors"), "replace")
        self.assertNotIn("text", run.call_args.kwargs)


class FocusRoutingPrefersTheExactTab(unittest.TestCase):
    def test_a_recorded_pane_wins_over_window_hunting(self):
        with patch("huginn.focus._platform") as plat:
            plat.pid_alive.return_value = True
            plat.discover_pane.return_value = None
            plat.focus_pane.return_value = type(
                "R", (), {"ok": True, "target": "WezTerm", "detail": "pane 7 focused"})()
            result = focus_session(claude_session(terminal=WEZ))
        plat.focus_pane.assert_called_once_with(WEZ)
        plat.focus_terminal.assert_not_called()
        self.assertEqual(result["target"], "WezTerm")

    def test_a_stale_pane_falls_back_to_raising_the_terminal(self):
        """Pane ids go stale in the ordinary course: the tab gets closed.

        Falling through matters more than reporting the failure -- a raised
        terminal is still closer to what the user asked for than a refusal.
        """
        with patch("huginn.focus._platform") as plat:
            plat.pid_alive.return_value = True
            plat.discover_pane.return_value = None
            plat.focus_pane.return_value = type(
                "R", (), {"ok": False, "target": None, "detail": "no such pane"})()
            plat.process_tty.return_value = None
            plat.children.return_value = []
            plat.parent.return_value = None
            plat.focus_terminal.return_value = type(
                "R", (), {"ok": True, "target": "WezTerm", "detail": "window raised"})()
            result = focus_session(claude_session(terminal=WEZ))
        plat.focus_terminal.assert_called_once()
        self.assertTrue(result["ok"])

    def test_an_unrecorded_session_is_discovered_from_the_terminal(self):
        """A tab that has been idle since Huginn started reports nothing.

        The hook only fires for a session that does something, so after any
        daemon restart every open tab is unrecorded until it next takes a turn.
        Waiting for that is the babysitting this exists to remove -- the
        terminal already knows, so it gets asked.
        """
        found = {"kind": "wezterm", "pane": "5"}
        with patch("huginn.focus._platform") as plat:
            plat.pid_alive.return_value = True
            plat.discover_pane.return_value = found
            plat.focus_pane.return_value = type(
                "R", (), {"ok": True, "target": "WezTerm", "detail": "pane 5 focused"})()
            result = focus_session(claude_session())
        plat.discover_pane.assert_called_once_with(r"C:\repo")
        plat.focus_pane.assert_called_once_with(found)
        self.assertEqual(result["target"], "WezTerm")

    def test_nothing_recorded_and_nothing_discovered_raises_a_window(self):
        with patch("huginn.focus._platform") as plat:
            plat.pid_alive.return_value = True
            plat.discover_pane.return_value = None
            plat.process_tty.return_value = None
            plat.children.return_value = []
            plat.parent.return_value = None
            plat.focus_terminal.return_value = type(
                "R", (), {"ok": True, "target": "Windows Terminal", "detail": "raised"})()
            focus_session(claude_session())
        plat.focus_pane.assert_not_called()
        plat.focus_terminal.assert_called_once()

    def test_a_stale_record_is_retried_by_discovery_before_giving_up(self):
        """The tab moved; the terminal still knows where it went."""
        found = {"kind": "wezterm", "pane": "9"}
        attempts = []

        def focus_pane(terminal):
            attempts.append(terminal)
            ok = terminal is found
            return type("R", (), {"ok": ok, "target": "WezTerm" if ok else None,
                                  "detail": "pane 9 focused" if ok else "no such pane"})()

        with patch("huginn.focus._platform") as plat:
            plat.pid_alive.return_value = True
            plat.discover_pane.return_value = found
            plat.focus_pane.side_effect = focus_pane
            result = focus_session(claude_session(terminal=WEZ))
        self.assertEqual(attempts, [WEZ, found])
        self.assertEqual(result["target"], "WezTerm")


class TheReducerRecordsIt(unittest.TestCase):
    def test_a_hook_payload_updates_the_session(self):
        from huginn.config import Config
        from huginn.model import Event
        from huginn.state import Reducer

        reducer = Reducer(Config({}))
        session = claude_session(session_id="sid-1")
        reducer.sessions[session.key] = session
        reducer.apply(Event("hook.claude", session.key, 0.0, "hook", {
            "event": "UserPromptSubmit",
            "data": {"session_id": "sid-1", "huginn_terminal": WEZ},
        }))
        self.assertEqual(session.terminal, WEZ)

    def test_a_later_report_replaces_an_earlier_one(self):
        """A session moved to a new pane keeps firing hooks; the newest wins."""
        from huginn.config import Config
        from huginn.model import Event
        from huginn.state import Reducer

        reducer = Reducer(Config({}))
        session = claude_session(session_id="sid-1", terminal=dict(WEZ))
        reducer.sessions[session.key] = session
        moved = {**WEZ, "pane": "9"}
        reducer.apply(Event("hook.claude", session.key, 0.0, "hook", {
            "event": "UserPromptSubmit",
            "data": {"session_id": "sid-1", "huginn_terminal": moved},
        }))
        self.assertEqual(session.terminal["pane"], "9")

    def test_the_field_survives_a_snapshot(self):
        """Jump must keep working across a daemon restart."""
        session = claude_session(terminal=WEZ)
        self.assertEqual(Session.from_dict(session.to_dict()).terminal, WEZ)


if __name__ == "__main__":
    unittest.main()
