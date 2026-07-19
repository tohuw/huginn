"""Auth-token gate on the localhost API (issue #5)."""
from __future__ import annotations

import unittest

from fastapi.testclient import TestClient

from huginn.config import Config
from huginn.daemon import Daemon
from huginn.server.app import create_app


def make_client(token: str = "secret-token") -> TestClient:
    daemon = Daemon(Config({}))
    daemon.token = token
    app = create_app(daemon)
    return TestClient(app)


class AuthTests(unittest.TestCase):
    def test_sessions_requires_token(self):
        c = make_client()
        self.assertEqual(c.get("/api/sessions").status_code, 401)

    def test_sessions_accepts_header_token(self):
        c = make_client()
        r = c.get("/api/sessions", headers={"X-Huginn-Token": "secret-token"})
        self.assertEqual(r.status_code, 200)

    def test_sessions_rejects_wrong_token(self):
        c = make_client()
        r = c.get("/api/sessions", headers={"X-Huginn-Token": "wrong"})
        self.assertEqual(r.status_code, 401)

    def test_hook_requires_token(self):
        c = make_client()
        r = c.post("/api/hook/claude/Stop", json={})
        self.assertEqual(r.status_code, 401)

    def test_hook_accepts_header_token(self):
        c = make_client()
        r = c.post("/api/hook/claude/Stop", json={},
                    headers={"X-Huginn-Token": "secret-token"})
        self.assertEqual(r.status_code, 200)

    def test_events_rejects_missing_token(self):
        c = make_client()
        r = c.get("/api/events")
        self.assertEqual(r.status_code, 401)

    def test_index_unauthenticated_and_bootstraps_token(self):
        c = make_client()
        r = c.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("secret-token", r.text)

    def test_static_unauthenticated(self):
        c = make_client()
        r = c.get("/static/app.js")
        self.assertEqual(r.status_code, 200)


if __name__ == "__main__":
    unittest.main()
