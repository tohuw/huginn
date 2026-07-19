"""Parser tests driven by real, redacted samples (tests/fixtures/) rather
than hand-written minimal dicts -- issue #22. A synthetic fixture can keep
passing after upstream field nesting changes in a way these can't, because
field names and nesting here are real, only values are redacted. See
tests/fixtures/PROVENANCE.md for what was captured from where.
"""
from __future__ import annotations

import json
import sqlite3
import unittest
from pathlib import Path

from huginn.sources.codex import _THREAD_COLS, _available_cols
from huginn.sources.transcript import ClaudeAnalyzer, CodexAnalyzer

FIXTURES = Path(__file__).parent / "fixtures"


def _load_jsonl(name: str) -> list[dict]:
    lines = []
    for line in (FIXTURES / name).read_text().splitlines():
        line = line.strip()
        if line:
            lines.append(json.loads(line))
    return lines


class ClaudeTranscriptFixtureTests(unittest.TestCase):
    def setUp(self):
        self.entries = _load_jsonl("claude_transcript_lines.jsonl")

    def test_real_shape_parses_without_crashing(self):
        an = ClaudeAnalyzer()
        changed = an.feed(self.entries)
        self.assertTrue(changed)

    def test_agent_spawn_and_stop_notification_tracked(self):
        # the fixture's Agent tool_use + queue-operation Stop is exactly the
        # structure issue #8's subagent tracking depends on.
        an = ClaudeAnalyzer()
        an.feed(self.entries)
        self.assertEqual(an.activity()["subagents"], {"done": 1})

    def test_tolerates_unknown_extra_key_on_every_entry(self):
        # a hypothetical new field Claude Code starts sending must not crash
        # the parser -- this is the actual "tolerate schema drift" claim.
        mutated = [{**e, "some_future_field_huginn_has_never_seen": {"nested": [1, 2, 3]}}
                  for e in self.entries]
        an = ClaudeAnalyzer()
        an.feed(mutated)   # must not raise

    def test_tolerates_missing_optional_fields(self):
        # gitBranch, version, cwd are read opportunistically -- drop them all
        # and confirm the analyzer degrades gracefully instead of KeyError-ing.
        optional = {"gitBranch", "version", "cwd", "permissionMode", "origin",
                   "promptSource", "userType", "effort", "requestId"}
        mutated = [{k: v for k, v in e.items() if k not in optional} for e in self.entries]
        an = ClaudeAnalyzer()
        an.feed(mutated)   # must not raise
        self.assertIsNone(an.git_branch)


class CodexRolloutFixtureTests(unittest.TestCase):
    def setUp(self):
        self.entries = _load_jsonl("codex_rollout_lines.jsonl")

    def test_real_shape_parses_without_crashing(self):
        an = CodexAnalyzer()
        changed = an.feed(self.entries)
        self.assertTrue(changed)

    def test_phase_and_token_extraction_from_real_shape(self):
        an = CodexAnalyzer()
        an.feed(self.entries)
        self.assertEqual(an.phase, "done")
        self.assertIsNotNone(an.tokens)
        self.assertEqual(an.last_agent_text, "[REDACTED TEXT]")

    def test_real_turn_replays_start_through_completion(self):
        an = CodexAnalyzer()
        phases = []
        for entry in self.entries:
            if an.feed([entry]) and an.phase:
                phases.append(an.phase)
        self.assertIn("working", phases)
        self.assertEqual(phases[-1], "done")

    def test_tolerates_unknown_extra_key(self):
        mutated = [{**e, "some_future_field": "x"} for e in self.entries]
        an = CodexAnalyzer()
        an.feed(mutated)   # must not raise

    def test_tolerates_missing_optional_payload_fields(self):
        def strip(e):
            e = dict(e)
            if "payload" in e:
                e["payload"] = {k: v for k, v in e["payload"].items()
                               if k in ("type", "message")}
            return e
        an = CodexAnalyzer()
        an.feed([strip(e) for e in self.entries])   # must not raise


class CodexSchemaFixtureTests(unittest.TestCase):
    """Real schema (DDL only, no redaction needed) vs. the column list the
    scanner actually requests -- catches drift a hand-written CREATE TABLE
    in a test file wouldn't."""

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.executescript((FIXTURES / "codex_state_db_schema.sql").read_text())

    def test_every_column_the_scanner_wants_exists_in_the_real_schema(self):
        available = _available_cols(self.conn)
        missing = set(_THREAD_COLS) - set(available)
        self.assertEqual(missing, set(),
                         f"scanner expects columns the real schema fixture doesn't have: {missing}")

    def test_thread_spawn_edges_table_present_in_real_schema(self):
        tables = {row[0] for row in
                 self.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertIn("thread_spawn_edges", tables)


if __name__ == "__main__":
    unittest.main()
