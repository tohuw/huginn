"""Blurb task lifecycle and stale-result protection."""
from __future__ import annotations

import asyncio
import time
import unittest
from unittest.mock import patch

from huginn.config import Config
from huginn.daemon import Daemon
from huginn.model import Session, SessionState


def session(state: SessionState) -> Session:
    return Session(
        key="claude:1", source="claude", session_id="s1", cwd="/tmp",
        name="work", state=state, state_since=time.time(),
        state_origin="hook", last_activity=time.time(),
    )


class _Provider:
    def __init__(self, during_call=None):
        self.during_call = during_call

    async def run_text(self, *args, **kwargs):
        if self.during_call:
            self.during_call()
        return "stale summary"


class BlurbTests(unittest.IsolatedAsyncioTestCase):
    async def test_state_change_replaces_pending_blurb_task(self):
        daemon = Daemon(Config({"llm": {"blurb_debounce_s": 60}}))
        s = session(SessionState.DONE)
        daemon.reducer.sessions[s.key] = s
        daemon.blurbs.request(s)
        task = daemon.blurbs._pending[s.key]

        s.state = SessionState.WORKING
        daemon.blurbs.request(s)
        await asyncio.sleep(0)

        self.assertTrue(task.cancelled())
        self.assertIn(s.key, daemon.blurbs._pending)
        self.assertIsNot(task, daemon.blurbs._pending[s.key])

    async def test_state_change_during_generation_discards_result(self):
        daemon = Daemon(Config({"llm": {"blurb_debounce_s": 0}}))
        s = session(SessionState.DONE)
        daemon.reducer.sessions[s.key] = s
        provider = _Provider(lambda: setattr(s, "state", SessionState.WORKING))

        with patch("huginn.llm.blurb.get_provider", return_value=provider):
            await daemon.blurbs._debounced(s.key, SessionState.DONE)

        self.assertIsNone(s.blurb)

    async def test_disabling_cancels_pending_blurbs(self):
        daemon = Daemon(Config({"llm": {"blurb_debounce_s": 60}}))
        s = session(SessionState.DONE)
        daemon.reducer.sessions[s.key] = s
        daemon.blurbs.request(s)
        task = daemon.blurbs._pending[s.key]

        daemon.cfg.update("llm", "enabled", False)
        daemon.blurbs.set_enabled(False)
        await asyncio.sleep(0)

        self.assertTrue(task.cancelled())
        self.assertEqual(daemon.blurbs._pending, {})


if __name__ == "__main__":
    unittest.main()
