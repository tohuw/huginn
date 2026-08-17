"""Static contracts for the console keeping itself current without a reload.

The console had three ways to fall behind and stay behind, all of which looked
identical to the user: a stale page that a manual refresh fixed.

1. The snapshot poll was additive only, so a `session.remove` missed while
   EventSource was reconnecting -- or emitted by a *previous* daemon, since a
   restart never replays them -- left a dead session on screen forever.
2. EventSource does not reconnect after an HTTP error, per spec. A daemon
   restart rotates the token, so the next stream answered 401 and the live feed
   was dead for the life of the page.
3. A background tab has its timers throttled to roughly once a minute, so the
   roster was stale at the exact moment somebody looked at it again.

Asserted against the source text, matching the other static tests here: this
file is loaded by a browser, and the suite has no browser.
"""
from __future__ import annotations

import unittest
from pathlib import Path


APP_JS = Path(__file__).parents[1] / "huginn" / "server" / "static" / "app.js"


class SnapshotReconcilesRemovals(unittest.TestCase):
    def setUp(self) -> None:
        self.source = APP_JS.read_text(encoding="utf-8")

    def test_absence_is_reconciled_when_the_daemon_says_it_counts(self):
        self.assertIn("if (data.complete) {", self.source)
        self.assertIn("if (!live.has(key)) removeCard(key);", self.source)

    def test_the_flag_gates_it_rather_than_the_client_deciding(self):
        """Only the daemon knows whether every source has scanned since boot.

        A client-side heuristic -- "absent twice in a row" -- would blank cards
        during a slow start, which is the failure the additive rule was written
        to avoid in the first place.
        """
        removal = self.source[self.source.index("const live = new Set("):]
        self.assertLess(removal.index("removeCard"), removal.index("upsertCard"),
                        "the removal sweep should be self-contained")
        self.assertIn('const live = new Set(data.sessions.map((s) => s.key));',
                      self.source)


class TheStreamRecoversOnItsOwn(unittest.TestCase):
    def setUp(self) -> None:
        self.source = APP_JS.read_text(encoding="utf-8")

    def test_a_closed_stream_is_reconnected(self):
        self.assertIn("if (es.readyState === EventSource.CLOSED) scheduleReconnect();",
                      self.source)

    def test_a_reconnect_refreshes_the_cookie_first(self):
        """The reason the stream died is usually that the token rotated."""
        block = self.source[self.source.index("function scheduleReconnect()"):]
        block = block[:block.index("\n}")]
        self.assertIn("refreshSession()", block)
        self.assertIn("connect();", block)

    def test_reconnects_back_off_and_are_not_stacked(self):
        block = self.source[self.source.index("function scheduleReconnect()"):]
        block = block[:block.index("\n}")]
        self.assertIn("if (reconnectTimer) return;", block)
        self.assertIn("RECONNECT_MAX_MS", block)

    def test_a_good_connection_resets_the_backoff(self):
        self.assertIn("reconnectDelay = 1000;", self.source)

    def test_connecting_twice_does_not_leave_two_live_streams(self):
        block = self.source[self.source.index("function connect()"):]
        self.assertIn("if (eventSource) eventSource.close();", block)

    def test_a_retry_in_progress_is_left_alone(self):
        """CONNECTING is EventSource's own retry; racing it doubles the stream."""
        self.assertNotIn("EventSource.CONNECTING) scheduleReconnect", self.source)


class ItRefreshesWhenLookedAt(unittest.TestCase):
    def setUp(self) -> None:
        self.source = APP_JS.read_text(encoding="utf-8")

    def test_returning_to_the_tab_resyncs(self):
        self.assertIn('document.addEventListener("visibilitychange"', self.source)
        self.assertIn('if (document.visibilityState !== "visible") return;',
                      self.source)

    def test_regaining_the_network_resyncs(self):
        self.assertIn('window.addEventListener("online"', self.source)

    def test_both_revive_a_dead_stream_rather_than_only_polling(self):
        for event in ('"visibilitychange"', '"online"'):
            start = self.source.index(f"addEventListener({event}")
            block = self.source[start:start + 400]
            with self.subTest(event=event):
                self.assertIn("snapshot();", block)
                self.assertIn("EventSource.CLOSED) connect();", block)


if __name__ == "__main__":
    unittest.main()
