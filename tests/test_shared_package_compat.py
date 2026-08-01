"""Backward compatibility of the paths corvidae was extracted from -- issue #42.

Plugins import ``huginn.model`` and ``huginn.sources.transcript`` directly, and
out-of-tree plugin distributions (e.g. huginn-cisco's Bedrock and Neo-Cortex)
import from upstream by module path. The extraction is only acceptable if it is
invisible to them, so these pin the old import paths and pin that they resolve to
the *same objects* -- not lookalike copies, which would break isinstance checks
and enum identity across the seam.
"""
from __future__ import annotations

import unittest

import corvidae
from huginn import model as huginn_model
from huginn.llm import context as huginn_context
from huginn.model import ATTENTION_STATES, STATE_RANK, Event, Session, SessionState
from huginn.sources import transcript as huginn_transcript
from huginn.sources.transcript import ATTACH_WINDOW, MAX_READ, ClaudeAnalyzer, CodexAnalyzer, Tail


class OriginalImportPathsTests(unittest.TestCase):
    def test_model_names_are_the_shared_objects_not_copies(self):
        self.assertIs(Session, corvidae.Session)
        self.assertIs(SessionState, corvidae.SessionState)
        self.assertIs(STATE_RANK, corvidae.STATE_RANK)
        self.assertIs(ATTENTION_STATES, corvidae.ATTENTION_STATES)

    def test_transcript_names_are_the_shared_objects_not_copies(self):
        self.assertIs(Tail, corvidae.Tail)
        self.assertIs(ClaudeAnalyzer, corvidae.ClaudeAnalyzer)
        self.assertIs(CodexAnalyzer, corvidae.CodexAnalyzer)
        self.assertEqual(ATTACH_WINDOW, corvidae.ATTACH_WINDOW)
        self.assertEqual(MAX_READ, corvidae.MAX_READ)

    def test_redact_secrets_is_the_shared_object_not_a_copy(self):
        self.assertIs(huginn_context.redact_secrets, corvidae.redact_secrets)

    def test_event_stayed_in_huginn(self):
        # Deliberately not shared: it is this daemon's bus envelope. Kept as a
        # test so a later "tidy the re-exports" pass doesn't move it by reflex.
        self.assertFalse(hasattr(corvidae, "Event"))
        self.assertIs(huginn_model.Event, Event)

    def test_private_helpers_context_relies_on_still_resolve(self):
        # huginn.llm.context imports these from the old path; they carry no
        # corvidae stability promise, but huginn is inside the version boundary.
        for name in ("_items", "_user_text", "_parse_lines", "_strip_meta", "_iso_ts"):
            with self.subTest(name=name):
                self.assertTrue(callable(getattr(huginn_transcript, name)))


if __name__ == "__main__":
    unittest.main()
