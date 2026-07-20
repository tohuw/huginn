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


if __name__ == "__main__":
    unittest.main()
