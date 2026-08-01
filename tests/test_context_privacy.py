"""Credential redaction at the shared session-evidence seam.

The pattern-level coverage for ``redact_secrets`` moved to corvidae with the
function itself (issue #42, packages/corvidae/tests/test_redact.py). What stays
here is huginn's side of the seam: proof that every path which turns transcript
bytes into evidence actually calls through to redaction.
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace

from huginn.llm.context import (
    digest_for_session,
    distill_claude,
    distill_codex,
    evidence_text,
    redact_secrets,
)
from huginn.model import SessionState


class EvidenceRedactionTests(unittest.TestCase):
    def test_redact_secrets_is_still_importable_from_its_original_path(self):
        # Plugins and forks import this name from here; the move to corvidae must
        # stay invisible to them -- issue #42.
        self.assertEqual(redact_secrets("token=do-not-send"), "[REDACTED]")

    def test_claude_user_and_tool_evidence_is_redacted(self):
        entries = [
            {"type": "user", "message": {"content": "use token=private-value"}},
            {"type": "assistant", "message": {"content": [{
                "type": "tool_use",
                "name": "Bash",
                "input": {"command": "curl -H 'Authorization: Bearer abc.def'"},
            }]}},
        ]

        lines = "\n".join(distill_claude(entries))

        self.assertNotIn("private-value", lines)
        self.assertNotIn("abc.def", lines)
        self.assertGreaterEqual(lines.count("[REDACTED]"), 2)

    def test_codex_agent_evidence_is_redacted(self):
        entries = [{
            "type": "event_msg",
            "payload": {"type": "agent_message", "message": "password=hunter2"},
        }]

        self.assertEqual(distill_codex(entries), ["assistant: [REDACTED]"])

    def test_digest_redacts_and_bounds_session_metadata(self):
        session = SimpleNamespace(
            name="agent-token=private-name",
            source="codex",
            state=SessionState.IDLE,
            state_since=0,
            cwd="/tmp/password=private-path",
            git_branch="secret=private-branch",
            model="gpt-5",
            transcript_path=None,
        )

        digest = digest_for_session(session)

        self.assertNotIn("private-name", digest)
        self.assertNotIn("private-path", digest)
        self.assertNotIn("private-branch", digest)

    def test_normal_evidence_is_preserved(self):
        self.assertEqual(evidence_text("feature/plugin-api"), "feature/plugin-api")


if __name__ == "__main__":
    unittest.main()
