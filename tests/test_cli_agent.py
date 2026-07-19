"""Agent-facing CLI commands use the daemon's stable semantic API."""
from __future__ import annotations

import io
import json
import unittest
from argparse import Namespace
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

from huginn import cli


SESSION = {
    "key": "codex:abc", "name": "huginn-abc", "source": "codex",
    "session_id": "abc", "state": "waiting_input", "state_since": 100.0,
    "attention": True, "cwd": "/tmp/huginn", "blurb": "Needs a decision",
    "last_prompt": "What next?",
}


class AgentCliTests(unittest.TestCase):
    def test_roster_attention_filters_and_formats_names(self):
        quiet = {**SESSION, "key": "codex:def", "name": "quiet", "attention": False}
        out = io.StringIO()
        with patch("huginn.cli._daemon_api", return_value={"sessions": [quiet, SESSION]}), \
                redirect_stdout(out):
            code = cli.cmd_roster(Namespace(attention=True, json=False))
        self.assertEqual(code, 0)
        self.assertIn("@huginn-abc", out.getvalue())
        self.assertNotIn("quiet", out.getvalue())

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


if __name__ == "__main__":
    unittest.main()
