"""Automatic-title lifecycle, budgets, caching, and provider failure circuits."""
from __future__ import annotations

import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from huginn import config
from huginn.config import Config
from huginn.daemon import Daemon
from huginn.model import Session, SessionState
from huginn.plugins import LLMProviderError


def session(state: SessionState) -> Session:
    return Session(
        key="claude:1", source="claude", session_id="s1", cwd="/tmp",
        name="work", state=state, state_since=time.time(),
        state_origin="hook", last_activity=time.time(),
    )


def enabled_config(**overrides) -> Config:
    values = {
        "enabled": True,
        "blurb_debounce_s": 0,
        "blurb_max_per_min": 6,
        "blurb_max_per_day": 200,
    }
    values.update(overrides)
    return Config({"llm": values})


class _Provider:
    name = "claude"
    default_blurb_model = "haiku"

    def __init__(self, during_call=None, error: BaseException | None = None):
        self.during_call = during_call
        self.error = error
        self.calls = 0

    def available(self):
        return None

    async def run_text(self, *args, **kwargs):
        self.calls += 1
        if self.during_call:
            self.during_call()
        if self.error:
            raise self.error
        return "useful automatic text"


class BlurbTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state_dir = Path(self.tmp.name) / "state"
        self.cache_dir = self.state_dir / "cache"
        self.patches = [
            patch.object(config, "STATE_DIR", self.state_dir),
            patch.object(config, "CACHE_DIR", self.cache_dir),
        ]
        for item in self.patches:
            item.start()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.tmp.cleanup()

    async def test_state_change_coalesces_instead_of_cancelling_pending_task(self):
        daemon = Daemon(enabled_config(blurb_debounce_s=60))
        s = session(SessionState.DONE)
        daemon.reducer.sessions[s.key] = s
        daemon.blurbs.request(s)
        task = daemon.blurbs._pending[s.key]

        s.state = SessionState.WORKING
        daemon.blurbs.request(s)
        await asyncio.sleep(0)

        self.assertFalse(task.cancelled())
        self.assertIs(task, daemon.blurbs._pending[s.key])
        self.assertIn(s.key, daemon.blurbs._rerun)
        daemon.blurbs.set_enabled(False)
        await asyncio.sleep(0)

    async def test_state_change_during_generation_discards_blurb_but_keeps_title(self):
        daemon = Daemon(enabled_config())
        s = session(SessionState.DONE)
        daemon.reducer.sessions[s.key] = s
        provider = _Provider(lambda: setattr(s, "state", SessionState.WORKING))

        with patch("huginn.llm.blurb.get_provider", return_value=provider):
            await daemon.blurbs._debounced(s.key, SessionState.DONE)

        self.assertEqual(s.title, "useful automatic text")
        self.assertEqual(s.title_origin, "guessed")
        self.assertIsNone(s.blurb)

    async def test_disabling_cancels_pending_blurbs(self):
        daemon = Daemon(enabled_config(blurb_debounce_s=60))
        s = session(SessionState.DONE)
        daemon.reducer.sessions[s.key] = s
        daemon.blurbs.request(s)
        task = daemon.blurbs._pending[s.key]

        daemon.cfg.update("llm", "enabled", False)
        daemon.blurbs.set_enabled(False)
        await asyncio.sleep(0)

        self.assertTrue(task.cancelled())
        self.assertEqual(daemon.blurbs._pending, {})
        self.assertEqual(daemon.blurbs._rerun, set())

    async def test_permanent_failure_opens_circuit_until_configuration_changes(self):
        daemon = Daemon(enabled_config())
        provider = _Provider(error=LLMProviderError("bad model", retryable=False))

        with patch("huginn.llm.blurb.get_provider", return_value=provider):
            self.assertIsNone(await daemon.blurbs._run_prompt("first"))
            self.assertIsNone(await daemon.blurbs._run_prompt("second"))
            self.assertEqual(provider.calls, 1)
            self.assertTrue(daemon.blurbs.status()["circuit"]["permanent"])

            daemon.cfg.update("llm", "blurb_model", "claude-haiku-new")
            self.assertIsNone(await daemon.blurbs._run_prompt("third"))
            self.assertEqual(provider.calls, 2)

    async def test_transient_failure_uses_exponential_backoff(self):
        daemon = Daemon(enabled_config())
        provider = _Provider(error=LLMProviderError("busy", retryable=True))

        with patch("huginn.llm.blurb.get_provider", return_value=provider):
            self.assertIsNone(await daemon.blurbs._run_prompt("first"))
            retry_at = daemon.blurbs.status()["circuit"]["retry_at"]
            self.assertGreater(retry_at, time.time())
            self.assertIsNone(await daemon.blurbs._run_prompt("second"))
            self.assertEqual(provider.calls, 1)

    async def test_exact_prompt_response_is_cached_without_another_budget_claim(self):
        daemon = Daemon(enabled_config())
        provider = _Provider()

        with patch("huginn.llm.blurb.get_provider", return_value=provider):
            first = await daemon.blurbs._run_prompt("same")
            second = await daemon.blurbs._run_prompt("same")

        self.assertEqual(first, second)
        self.assertEqual(provider.calls, 1)
        self.assertEqual(daemon.blurbs.status()["budget"]["used"], 1)

    async def test_daily_budget_persists_across_daemon_restarts(self):
        provider = _Provider()
        first = Daemon(enabled_config(blurb_max_per_day=1))
        with patch("huginn.llm.blurb.get_provider", return_value=provider):
            self.assertIsNotNone(await first.blurbs._run_prompt("first"))

        second = Daemon(enabled_config(blurb_max_per_day=1))
        with patch("huginn.llm.blurb.get_provider", return_value=provider):
            self.assertIsNone(await second.blurbs._run_prompt("second"))

        self.assertEqual(provider.calls, 1)
        self.assertEqual(second.blurbs.status()["budget"]["remaining"], 0)


if __name__ == "__main__":
    unittest.main()
