"""Daemon-level snapshot round trip: sessions (#7) + hook hit counts (#2)
share one file on disk, written/restored together."""
from __future__ import annotations

import asyncio
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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

    def test_failed_write_stays_dirty_for_retry(self):
        d = Daemon(Config({}))
        d.mark_dirty()
        with patch.object(d, "_write_snapshot", side_effect=OSError("disk busy")):
            self.assertFalse(d._flush_snapshot_if_dirty())
        self.assertTrue(d._dirty)

        with patch.object(d, "_write_snapshot") as write:
            self.assertTrue(d._flush_snapshot_if_dirty())
        write.assert_called_once_with()
        self.assertFalse(d._dirty)

    def test_second_daemon_does_not_rotate_live_credentials(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            port = listener.getsockname()[1]
            token_path = Path(self.tmp.name) / "token"
            token_path.write_text("live-token")
            cfg = Config({"server": {"host": "127.0.0.1", "port": port}})
            with patch("huginn.daemon.config.TOKEN_PATH", token_path), \
                 patch("huginn.daemon.config.write_token") as write_token:
                with self.assertRaisesRegex(OSError, "address already in use"):
                    asyncio.run(Daemon(cfg).run(open_browser=False))
            write_token.assert_not_called()
            self.assertEqual(token_path.read_text(), "live-token")


if __name__ == "__main__":
    unittest.main()
