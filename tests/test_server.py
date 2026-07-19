"""Auth-token gate on the localhost API (issue #5)."""
from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from huginn.config import Config
from huginn.daemon import Daemon
from huginn.server.app import create_app


def make_client(token: str = "secret-token") -> TestClient:
    daemon = Daemon(Config({}))
    daemon.token = token
    app = create_app(daemon)
    # base_url controls the Host header TestClient sends; require_local_origin
    # rejects anything but 127.0.0.1/localhost, so this must match.
    return TestClient(app, base_url="http://127.0.0.1")


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

    def test_index_unauthenticated_and_carries_no_secret(self):
        # The shell must be safely fetchable by any local process -- the
        # token only ever reaches the browser via a URL fragment, which the
        # server never sees (issue #23).
        c = make_client()
        r = c.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("secret-token", r.text)

    def test_session_sets_httponly_cookie_and_enables_cookie_auth(self):
        c = make_client()
        r = c.post("/api/session", headers={"X-Huginn-Token": "secret-token"})
        self.assertEqual(r.status_code, 200)
        set_cookie = r.headers.get("set-cookie", "")
        self.assertIn("huginn_token=secret-token", set_cookie)
        self.assertIn("HttpOnly", set_cookie)
        self.assertIn("samesite=strict", set_cookie.lower())
        # No header this time -- the cookie the client just stored should carry it.
        r2 = c.get("/api/sessions")
        self.assertEqual(r2.status_code, 200)

    def test_session_requires_token(self):
        c = make_client()
        self.assertEqual(c.post("/api/session").status_code, 401)

    def test_query_param_token_no_longer_accepted(self):
        c = make_client()
        r = c.get("/api/sessions?token=secret-token")
        self.assertEqual(r.status_code, 401)

    def test_mismatched_host_rejected(self):
        c = make_client()
        r = c.get("/api/sessions", headers={
            "X-Huginn-Token": "secret-token", "Host": "evil.example"})
        self.assertEqual(r.status_code, 400)

    def test_cross_origin_request_rejected(self):
        c = make_client()
        r = c.get("/api/sessions", headers={
            "X-Huginn-Token": "secret-token", "Origin": "http://evil.example"})
        self.assertEqual(r.status_code, 403)

    def test_missing_origin_header_allowed(self):
        # curl / huginn-hook don't send an Origin header at all.
        c = make_client()
        r = c.get("/api/sessions", headers={"X-Huginn-Token": "secret-token"})
        self.assertEqual(r.status_code, 200)

    def test_static_unauthenticated(self):
        c = make_client()
        r = c.get("/static/app.js")
        self.assertEqual(r.status_code, 200)

    def test_hook_stats_counts_by_source_and_event(self):
        c = make_client()
        headers = {"X-Huginn-Token": "secret-token"}
        c.post("/api/hook/claude/Stop", json={}, headers=headers)
        c.post("/api/hook/claude/Stop", json={}, headers=headers)
        c.post("/api/hook/codex/SessionStart", json={}, headers=headers)
        r = c.get("/api/hook-stats", headers=headers)
        self.assertEqual(r.json()["hits"], {"claude.Stop": 2, "codex.SessionStart": 1})

    def test_hook_stats_requires_token(self):
        c = make_client()
        self.assertEqual(c.get("/api/hook-stats").status_code, 401)

    def test_chat_rejection_uses_error_status(self):
        c = make_client()
        with patch("huginn.llm.chat.start_chat", new=AsyncMock(
                return_value={"ok": False, "error": "a chat is already running"})):
            r = c.post(
                "/api/chat", json={"question": "status?"},
                headers={"X-Huginn-Token": "secret-token"},
            )
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["detail"], "a chat is already running")


if __name__ == "__main__":
    unittest.main()
