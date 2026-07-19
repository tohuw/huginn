"""Daemon-level snapshot round trip: sessions (#7) + hook hit counts (#2)
share one file on disk, written/restored together."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from huginn import config
from huginn.config import Config
from huginn.daemon import Daemon
from huginn.model import Session, SessionState


class DaemonSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._orig_state_dir = config.STATE_DIR
        config.STATE_DIR = Path(self.tmp.name)

    def tearDown(self):
        config.STATE_DIR = self._orig_state_dir
        self.tmp.cleanup()

    def test_round_trip(self):
        d1 = Daemon(Config({}))
        d1.reducer.sessions["claude:1"] = Session(
            key="claude:1", source="claude", session_id="s1", cwd="/tmp", name="p",
            state=SessionState.WORKING, state_since=1.0, state_origin="statusfile",
            last_activity=1.0)
        d1.record_hook_hit("claude", "Stop")
        d1.record_hook_hit("claude", "Stop")
        d1._write_snapshot()

        d2 = Daemon(Config({}))
        d2._restore_snapshot()
        self.assertIn("claude:1", d2.reducer.sessions)
        self.assertEqual(d2.hook_hits, {"claude.Stop": 2})

    def test_restore_missing_file_is_a_noop(self):
        d = Daemon(Config({}))
        d._restore_snapshot()   # no sessions.json written yet
        self.assertEqual(d.reducer.sessions, {})
        self.assertEqual(d.hook_hits, {})


if __name__ == "__main__":
    unittest.main()
