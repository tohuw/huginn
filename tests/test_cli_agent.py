"""Agent-facing CLI commands use the daemon's stable semantic API."""
from __future__ import annotations

import io
import json
import tempfile
import unittest
from argparse import Namespace
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from huginn import cli


SESSION = {
    "key": "codex:abc", "name": "huginn-abc", "source": "codex",
    "session_id": "abc", "state": "waiting_input", "state_since": 100.0,
    "attention": True, "cwd": "/tmp/huginn", "blurb": "Needs a decision",
    "last_prompt": "What next?",
}


class AgentCliTests(unittest.TestCase):
    def test_demo_opens_privacy_safe_roster_without_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            (state / "port").write_text("47123")
            with patch("huginn.cli.config.STATE_DIR", state), \
                 patch("webbrowser.open") as opened:
                self.assertEqual(cli.cmd_demo(Namespace()), 0)
            opened.assert_called_once_with("http://127.0.0.1:47123/?demo=1")

    def test_roster_attention_filters_and_formats_names(self):
        quiet = {**SESSION, "key": "codex:def", "name": "quiet", "attention": False}
        out = io.StringIO()
        with patch("huginn.cli._daemon_api", return_value={"sessions": [quiet, SESSION]}), \
                redirect_stdout(out):
            code = cli.cmd_roster(Namespace(attention=True, json=False))
        self.assertEqual(code, 0)
        self.assertIn("@huginn-abc", out.getvalue())
        self.assertNotIn("quiet", out.getvalue())

    def test_triage_prints_worktree_contention(self):
        payload = {
            "triage": {
                "verdict": {"level": "contention", "headline": "1 worktree has competing sessions"},
                "contentions": [{
                    "worktree": "/tmp/project",
                    "sessions": [{"name": "alpha"}, {"name": "beta"}],
                }],
            },
        }
        out = io.StringIO()

        with patch("huginn.cli._daemon_api", return_value=payload), redirect_stdout(out):
            code = cli.cmd_triage(Namespace(json=False))

        self.assertEqual(code, 0)
        self.assertIn("@alpha, @beta", out.getvalue())

    def test_inspect_returns_distilled_tail(self):
        def api(path, method="GET"):
            if path == "/api/sessions":
                return {"sessions": [SESSION]}
            return {"lines": ["user: status?", "assistant: waiting"]}

        out = io.StringIO()
        args = Namespace(target="@huginn-abc", attention=False, lines=30, json=False)
        with patch("huginn.cli._daemon_api", side_effect=api), redirect_stdout(out):
            code = cli.cmd_inspect(args)
        self.assertEqual(code, 0)
        self.assertIn("Needs a decision", out.getvalue())
        self.assertIn("assistant: waiting", out.getvalue())

    def test_inspect_json_is_machine_readable(self):
        def api(path, method="GET"):
            return {"sessions": [SESSION]} if path == "/api/sessions" else {"lines": []}

        out = io.StringIO()
        args = Namespace(target="huginn-abc", attention=False, lines=10, json=True)
        with patch("huginn.cli._daemon_api", side_effect=api), redirect_stdout(out):
            self.assertEqual(cli.cmd_inspect(args), 0)
        self.assertEqual(json.loads(out.getvalue())["sessions"][0]["name"], "huginn-abc")

    def test_inspect_requires_target_or_attention(self):
        err = io.StringIO()
        args = Namespace(target=None, attention=False, lines=30, json=False)
        with redirect_stderr(err):
            self.assertEqual(cli.cmd_inspect(args), 2)
        self.assertIn("requires @name or --attention", err.getvalue())

    def test_history_prints_recorded_transitions(self):
        def api(path, method="GET"):
            if path == "/api/sessions":
                return {"sessions": [SESSION]}
            return {"key": "codex:abc", "transitions": [
                {"ts": 100.0, "from": "working", "to": "done", "origin": "poll"},
                {"ts": 105.0, "from": "done", "to": "working", "origin": "poll"},
            ]}

        out = io.StringIO()
        args = Namespace(target="@huginn-abc", json=False)
        with patch("huginn.cli._daemon_api", side_effect=api), redirect_stdout(out):
            code = cli.cmd_history(args)
        self.assertEqual(code, 0)
        lines = out.getvalue().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertIn("working", lines[0])
        self.assertIn("done", lines[0])

    def test_history_json_is_machine_readable(self):
        def api(path, method="GET"):
            if path == "/api/sessions":
                return {"sessions": [SESSION]}
            return {"key": "codex:abc", "transitions": [
                {"ts": 100.0, "from": "working", "to": "done", "origin": "poll"},
            ]}

        out = io.StringIO()
        args = Namespace(target="huginn-abc", json=True)
        with patch("huginn.cli._daemon_api", side_effect=api), redirect_stdout(out):
            self.assertEqual(cli.cmd_history(args), 0)
        body = json.loads(out.getvalue())
        self.assertEqual(body["key"], "codex:abc")
        self.assertEqual(body["transitions"][0]["to"], "done")

    def test_history_reports_no_transitions(self):
        def api(path, method="GET"):
            return {"sessions": [SESSION]} if path == "/api/sessions" else {"transitions": []}

        out = io.StringIO()
        args = Namespace(target="@huginn-abc", json=False)
        with patch("huginn.cli._daemon_api", side_effect=api), redirect_stdout(out):
            code = cli.cmd_history(args)
        self.assertEqual(code, 0)
        self.assertIn("no recorded transitions", out.getvalue())

    def test_focus_resolves_name_before_posting(self):
        calls = []

        def api(path, method="GET"):
            calls.append((path, method))
            return {"sessions": [SESSION]} if path == "/api/sessions" else {"ok": True}

        with patch("huginn.cli._daemon_api", side_effect=api), redirect_stdout(io.StringIO()):
            self.assertEqual(cli.cmd_focus(Namespace(target="@huginn-abc")), 0)
        self.assertEqual(calls[-1], ("/api/sessions/codex%3Aabc/focus", "POST"))

    def test_authority_targets_resolved_session(self):
        calls = []

        def api(path, method="GET", body=None, **_kwargs):
            calls.append((path, method, body))
            if path == "/api/sessions":
                return {"sessions": [SESSION]}
            return {"level": "steer"}

        args = Namespace(target="@huginn-abc", level="steer")
        with patch("huginn.cli._daemon_api", side_effect=api), redirect_stdout(io.StringIO()):
            self.assertEqual(cli.cmd_authority(args), 0)

        self.assertEqual(calls[-1], (
            "/api/sessions/codex%3Aabc/authority",
            "PUT",
            {"level": "steer"},
        ))

    def test_send_requires_yes_and_confirms_exact_preview(self):
        calls = []

        def api(path, method="GET", body=None, **_kwargs):
            calls.append((path, method, body))
            if path == "/api/sessions":
                return {"sessions": [SESSION]}
            if path.endswith("/steering/preview"):
                return {"confirmation_id": "confirm-1", "summary": "Send exact line"}
            return {"ok": True, "action": "send"}

        args = Namespace(target="@huginn-abc", instruction=["  exact input  "])
        with patch("huginn.cli._daemon_api", side_effect=api), \
             patch("builtins.input", return_value="yes"), \
             redirect_stdout(io.StringIO()):
            self.assertEqual(cli.cmd_send(args), 0)

        self.assertEqual(calls[1][2], {
            "action": "send",
            "instruction": "  exact input  ",
        })
        self.assertEqual(calls[2][2], {
            "confirmation_id": "confirm-1",
            "confirmed": True,
        })

    def test_install_and_uninstall_agent_reach_every_platform_backend(self):
        """issue #39: `uninstall-agent` must work on every platform install
        supports, not just the launchd one it started as."""
        from huginn import agent_install
        for platform in ("darwin", "linux", "win32"):
            with self.subTest(platform=platform):
                backend = agent_install.get_login_agent(platform)
                self.assertIsNotNone(backend)
                with patch.object(agent_install.sys, "platform", platform), \
                        patch.object(type(backend), "install", return_value=0) as install, \
                        patch.object(type(backend), "uninstall", return_value=0) as uninstall:
                    self.assertEqual(cli.cmd_install_agent(Namespace()), 0)
                    self.assertEqual(cli.cmd_uninstall_agent(Namespace()), 0)
                install.assert_called_once_with()
                uninstall.assert_called_once_with()

    def test_agent_commands_report_an_unsupported_platform(self):
        from huginn import agent_install
        err = io.StringIO()
        with patch.object(agent_install.sys, "platform", "sunos5"), redirect_stderr(err):
            self.assertEqual(cli.cmd_install_agent(Namespace()), 2)
            self.assertEqual(cli.cmd_uninstall_agent(Namespace()), 2)
        self.assertIn("not supported on sunos5", err.getvalue())

    def test_send_cancellation_is_consumed_server_side(self):
        calls = []

        def api(path, method="GET", body=None, **_kwargs):
            calls.append((path, method, body))
            if path == "/api/sessions":
                return {"sessions": [SESSION]}
            if path.endswith("/steering/preview"):
                return {"confirmation_id": "confirm-1", "summary": "Send exact line"}
            return {"ok": False, "cancelled": True}

        args = Namespace(target="@huginn-abc", instruction=["continue"])
        with patch("huginn.cli._daemon_api", side_effect=api), \
             patch("builtins.input", return_value="no"), \
             redirect_stderr(io.StringIO()):
            self.assertEqual(cli.cmd_send(args), 1)

        self.assertEqual(calls[2][2]["confirmed"], False)


class ConsoleFreeStartupTests(unittest.TestCase):
    """A pythonw daemon has no standard streams and must still start.

    ``install-agent`` registers pythonw.exe precisely so no console window
    appears at login, and uvicorn's log config calls ``sys.stdout.isatty()``.
    With sys.stdout None that raised before the daemon bound its port, so
    start-at-login on Windows failed while a hand-started daemon worked.
    """

    def test_missing_streams_are_bound_before_anything_uses_them(self):
        with patch.object(cli.sys, "stdout", None), patch.object(cli.sys, "stderr", None):
            cli._bind_missing_std_streams()
            self.assertIsNotNone(cli.sys.stdout)
            self.assertIsNotNone(cli.sys.stderr)
            # What uvicorn actually calls while building its formatter. The
            # answer does not matter -- on Windows devnull is a character
            # device and says True, which only costs colour codes nobody
            # reads. Not raising is the whole point.
            self.assertIsInstance(cli.sys.stdout.isatty(), bool)
            cli.sys.stdout.write("discarded")
            cli.sys.stderr.write("discarded")

    def test_existing_streams_are_left_alone(self):
        sentinel = io.StringIO()
        with patch.object(cli.sys, "stdout", sentinel), patch.object(cli.sys, "stderr", sentinel):
            cli._bind_missing_std_streams()
            self.assertIs(cli.sys.stdout, sentinel)
            self.assertIs(cli.sys.stderr, sentinel)


if __name__ == "__main__":
    unittest.main()
