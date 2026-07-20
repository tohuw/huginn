"""Credential redaction at the shared session-evidence seam."""
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
    def test_common_credentials_are_redacted(self):
        samples = (
            "key " + "AKIA" + "IOSFODNN7EXAMPLE",
            "Authorization: Bearer abc.def-ghi_123",
            "token=do-not-send",
            "github_" + "pat_1234567890abcdefghijklmnop",
            "https://user:password@example.com/path",
            "eyJabcdefghijk.abcdefghijk.abcdefghijk",
        )
        for sample in samples:
            with self.subTest(sample=sample):
                redacted = redact_secrets(sample)
                self.assertIn("[REDACTED]", redacted)
                self.assertNotIn("do-not-send", redacted)

    def test_private_key_marker_redacts_whole_evidence_item(self):
        value = "before -----BEGIN PRIVATE KEY----- private material"
        self.assertEqual(redact_secrets(value), "[REDACTED PRIVATE KEY]")

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
