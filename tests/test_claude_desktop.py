"""Claude Desktop activity ignores Chromium's idle housekeeping writes."""
from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from huginn.model import SessionState
from huginn.sources import claude_desktop


class ClaudeDesktopTests(unittest.TestCase):
    def test_idle_chromium_storage_writes_do_not_mean_working(self):
        with tempfile.TemporaryDirectory() as tmp:
            support = Path(tmp)
            noisy = [support / "DIPS-wal", support / "Local Storage/leveldb/000001.log"]
            for path in noisy:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.touch()
            with patch.object(claude_desktop, "APP_SUPPORT", support), \
                 patch.object(claude_desktop, "_app_running", return_value=True):
                session = claude_desktop.scan()
        self.assertIsNotNone(session)
        self.assertEqual(session.state, SessionState.IDLE)

    def test_recent_indexeddb_write_means_working(self):
        with tempfile.TemporaryDirectory() as tmp:
            support = Path(tmp)
            activity = support / "IndexedDB/https_claude.ai_0.indexeddb.leveldb/000001.log"
            activity.parent.mkdir(parents=True)
            activity.touch()
            os.utime(activity, (time.time(), time.time()))
            with patch.object(claude_desktop, "APP_SUPPORT", support), \
                 patch.object(claude_desktop, "_app_running", return_value=True):
                session = claude_desktop.scan()
        self.assertIsNotNone(session)
        self.assertEqual(session.state, SessionState.ACTIVE)


if __name__ == "__main__":
    unittest.main()
