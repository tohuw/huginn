"""Diagnostics registry: redaction, rate-limited logging, and real
background-loop failures becoming visible -- issue #15."""
from __future__ import annotations

import asyncio
import contextlib
import time
import unittest
from unittest.mock import patch

from huginn.config import Config
from huginn.daemon import Daemon
from huginn.diagnostics import Diagnostics
from huginn.model import Event


class DiagnosticsRegistryTests(unittest.TestCase):
    def test_ok_and_error_tracked_per_source(self):
        d = Diagnostics()
        d.ok("codex_poller")
        d.error("blurb", RuntimeError("boom"))
        snap = d.snapshot()
        self.assertIsNotNone(snap["codex_poller"]["last_success"])
        self.assertIsNone(snap["codex_poller"]["last_error_class"])
        self.assertEqual(snap["blurb"]["last_error_class"], "RuntimeError")
        self.assertEqual(snap["blurb"]["error_count"], 1)

    def test_error_count_accumulates(self):
        d = Diagnostics()
        for _ in range(5):
            d.error("codex_poller", ValueError("x"))
        self.assertEqual(d.snapshot()["codex_poller"]["error_count"], 5)

    def test_snapshot_never_contains_exception_message_text(self):
        d = Diagnostics()
        secret = "prompt: how do I exfiltrate /etc/passwd"
        d.error("blurb", RuntimeError(secret))
        snap_text = repr(d.snapshot())
        self.assertNotIn(secret, snap_text)
        self.assertNotIn("passwd", snap_text)

    def test_repeated_failures_are_log_rate_limited(self):
        d = Diagnostics()
        with self.assertLogs("huginn.diagnostics", level="ERROR") as logs:
            d.error("codex_poller", RuntimeError("first"))
            d.error("codex_poller", RuntimeError("second"))   # within the window
        self.assertEqual(len(logs.records), 1, "second failure logged before the rate-limit window")
        self.assertEqual(d.snapshot()["codex_poller"]["error_count"], 2, "count still tracks both")

    def test_failure_logs_after_the_rate_limit_window_elapses(self):
        d = Diagnostics()
        with self.assertLogs("huginn.diagnostics", level="ERROR") as logs:
            d.error("codex_poller", RuntimeError("first"))
            d.sources["codex_poller"]._last_logged -= 31   # simulate window elapsed
            d.error("codex_poller", RuntimeError("second"))
        self.assertEqual(len(logs.records), 2)


class BackgroundLoopFailureVisibilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_codex_poller_failure_becomes_visible_in_health(self):
        daemon = Daemon(Config({"codex": {"poll_s": 0.01}}))
        with patch("huginn.daemon.codex.scan_with_status", side_effect=RuntimeError("db locked")):
            task = asyncio.create_task(daemon.codex_poller())
            for _ in range(50):
                if daemon.diagnostics.snapshot().get("codex_poller", {}).get("error_count"):
                    break
                await asyncio.sleep(0.02)
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        health = daemon.diagnostics.snapshot()["codex_poller"]
        self.assertGreaterEqual(health["error_count"], 1)
        self.assertEqual(health["last_error_class"], "RuntimeError")

    async def test_reducer_loop_failure_becomes_visible_and_does_not_crash(self):
        daemon = Daemon(Config({}))
        with patch.object(daemon.reducer, "apply", side_effect=RuntimeError("bad event")):
            task = asyncio.create_task(daemon.reducer_loop())
            daemon.bus.emit(Event("claude.file", "k", time.time(), "test", {}))
            for _ in range(50):
                if daemon.diagnostics.snapshot().get("reducer", {}).get("error_count"):
                    break
                await asyncio.sleep(0.02)
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        health = daemon.diagnostics.snapshot()["reducer"]
        self.assertGreaterEqual(health["error_count"], 1)
        self.assertEqual(health["last_error_class"], "RuntimeError")


if __name__ == "__main__":
    unittest.main()
