"""Best-effort ChatGPT Desktop discovery without reading conversation data."""
from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from huginn.model import SessionState
from huginn.sources import chatgpt_desktop


class ChatGPTDesktopTests(unittest.TestCase):
    def test_absent_process_has_no_tile(self):
        with patch("huginn.sources.chatgpt_desktop._app_pids", return_value=[]):
            self.assertIsNone(chatgpt_desktop.scan())

    def test_recent_local_activity_marks_app_working(self):
        with tempfile.TemporaryDirectory() as tmp:
            support = Path(tmp)
            log = support / "desktop.log"
            log.write_text("activity")
            with patch("huginn.sources.chatgpt_desktop._app_pids", return_value=[42]), \
                 patch("huginn.sources.chatgpt_desktop._support_dirs", return_value=[support]):
                session = chatgpt_desktop.scan()
        self.assertIsNotNone(session)
        self.assertEqual(session.state, SessionState.WORKING)
        self.assertEqual(session.pid, 42)
        self.assertEqual(session.source, "chatgpt-desktop")

    def test_old_activity_keeps_presence_but_marks_idle(self):
        with patch("huginn.sources.chatgpt_desktop._app_pids", return_value=[42]), \
             patch("huginn.sources.chatgpt_desktop._latest_activity",
                   return_value=time.time() - chatgpt_desktop.ACTIVE_S - 1):
            session = chatgpt_desktop.scan()
        self.assertEqual(session.state, SessionState.IDLE)


if __name__ == "__main__":
    unittest.main()
