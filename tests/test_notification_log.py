"""Opt-in raw-Notification debug log (issue #1 scaffolding)."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

import huginn.server.app as app_module
from huginn.config import Config
from huginn.daemon import Daemon
from huginn.server.app import create_app


def make_client(debug_log: bool) -> TestClient:
    cfg = Config({"patterns": {"debug_log": debug_log}})
    daemon = Daemon(cfg)
    daemon.token = "t"
    app = create_app(daemon)
    return TestClient(app)


class NotificationDebugLogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.log_path = Path(self.tmp.name) / "notifications.log"
        self._orig = app_module.NOTIFICATIONS_LOG
        app_module.NOTIFICATIONS_LOG = self.log_path

    def tearDown(self):
        app_module.NOTIFICATIONS_LOG = self._orig
        self.tmp.cleanup()

    def test_logs_message_when_enabled(self):
        c = make_client(debug_log=True)
        c.post("/api/hook/claude/Notification", json={"message": "needs your permission"},
              headers={"X-Huginn-Token": "t"})
        line = json.loads(self.log_path.read_text().splitlines()[0])
        self.assertEqual(line["source"], "claude")
        self.assertEqual(line["message"], "needs your permission")

    def test_silent_when_disabled(self):
        c = make_client(debug_log=False)
        c.post("/api/hook/claude/Notification", json={"message": "needs your permission"},
              headers={"X-Huginn-Token": "t"})
        self.assertFalse(self.log_path.exists())


if __name__ == "__main__":
    unittest.main()
