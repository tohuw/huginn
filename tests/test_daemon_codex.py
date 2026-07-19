"""Codex polling reconciliation behavior."""
from __future__ import annotations

import unittest
from unittest.mock import patch

from huginn.config import Config
from huginn.daemon import Daemon
from huginn.model import Session, SessionState


def codex_session(key: str) -> Session:
    return Session(
        key=key, source="codex", session_id=key.removeprefix("codex:"),
        cwd="/tmp", name="codex", state=SessionState.DONE,
        state_since=1, state_origin="poll", last_activity=1,
    )


class CodexPollingTests(unittest.TestCase):
    def test_complete_scan_reconciles_missing_session(self):
        daemon = Daemon(Config({}))
        stale = codex_session("codex:stale")
        current = codex_session("codex:current")
        daemon.reducer.sessions[stale.key] = stale
        with patch("huginn.daemon.codex.scan_with_status",
                   return_value=([current], True)):
            daemon._poll_codex_once()
        events = [daemon.bus.events.get_nowait(), daemon.bus.events.get_nowait()]
        self.assertEqual([event.kind for event in events],
                         ["codex.thread", "codex.missing"])
        self.assertEqual(events[1].session_key, stale.key)

    def test_failed_scan_does_not_reconcile(self):
        daemon = Daemon(Config({}))
        daemon.reducer.sessions["codex:keep"] = codex_session("codex:keep")
        with patch("huginn.daemon.codex.scan_with_status",
                   return_value=([], False)):
            daemon._poll_codex_once()
        self.assertTrue(daemon.bus.events.empty())

    def test_native_scan_does_not_reconcile_wsl_codex(self):
        daemon = Daemon(Config({}))
        wsl = codex_session("wsl:Ubuntu:codex:keep")
        daemon.reducer.sessions[wsl.key] = wsl
        with patch("huginn.daemon.codex.scan_with_status", return_value=([], True)):
            daemon._poll_codex_once()
        self.assertTrue(daemon.bus.events.empty())


if __name__ == "__main__":
    unittest.main()
