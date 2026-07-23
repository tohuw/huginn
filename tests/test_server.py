"""Auth-token gate on the localhost API (issue #5)."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from huginn import config as config_module
from huginn.config import Config
from huginn.daemon import Daemon
from huginn.model import Session, SessionState
from huginn.server.app import create_app


def make_client(token: str = "secret-token") -> TestClient:
    return make_client_with_daemon(token)[0]


def make_client_with_daemon(token: str = "secret-token") -> tuple[TestClient, Daemon]:
    daemon = Daemon(Config({}))
    daemon.token = token
    daemon.refresh_token = "persistent-refresh-token"
    app = create_app(daemon)
    # base_url controls the Host header TestClient sends; require_local_origin
    # rejects anything but 127.0.0.1/localhost, so this must match.
    return TestClient(app, base_url="http://127.0.0.1"), daemon


class AuthTests(unittest.TestCase):
    @staticmethod
    def _add_terminal_session(daemon: Daemon) -> Session:
        session = Session(
            key="codex:thread-1",
            source="codex",
            session_id="thread-1",
            cwd="/tmp/project",
            name="project-thread",
            entrypoint="cli",
        )
        daemon.reducer.sessions[session.key] = session
        return session

    def test_sessions_requires_token(self):
        c = make_client()
        self.assertEqual(c.get("/api/sessions").status_code, 401)

    def test_sessions_accepts_header_token(self):
        c = make_client()
        r = c.get("/api/sessions", headers={"X-Huginn-Token": "secret-token"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["triage"]["verdict"]["level"], "clear")

    def test_activity_probes_sources_when_roster_is_empty(self):
        c = make_client()
        with patch("huginn.sources.claude_code.scan", return_value=[]), \
             patch("huginn.sources.codex.scan_with_status", return_value=([object()], True)):
            r = c.get("/api/activity", headers={"X-Huginn-Token": "secret-token"})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["agents_running"])

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
        self.assertIn("huginn_refresh=persistent-refresh-token", set_cookie)
        # No header this time -- the cookie the client just stored should carry it.
        r2 = c.get("/api/sessions")
        self.assertEqual(r2.status_code, 200)

    def test_session_requires_token(self):
        c = make_client()
        self.assertEqual(c.post("/api/session").status_code, 401)

    def test_refresh_rotates_expired_browser_cookie(self):
        c, daemon = make_client_with_daemon("old-token")
        self.assertEqual(c.post("/api/session", headers={
            "X-Huginn-Token": "old-token"}).status_code, 200)
        daemon.token = "new-token"
        self.assertEqual(c.get("/api/sessions").status_code, 401)
        self.assertEqual(c.post("/api/session/refresh").status_code, 200)
        self.assertEqual(c.get("/api/sessions").status_code, 200)

    def test_refresh_requires_refresh_cookie(self):
        c = make_client()
        self.assertEqual(c.post("/api/session/refresh").status_code, 401)

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

    def test_dismiss_removes_an_ended_session_and_broadcasts_removal(self):
        c, daemon = make_client_with_daemon()
        session = self._add_terminal_session(daemon)
        session.state = SessionState.ENDED
        events = []
        daemon.bus.broadcast = lambda event, data: events.append((event, data))
        headers = {"X-Huginn-Token": "secret-token"}

        r = c.post("/api/sessions/codex%3Athread-1/dismiss", headers=headers)

        self.assertEqual(r.status_code, 200)
        self.assertNotIn(session.key, daemon.reducer.sessions)
        self.assertIn(("session.remove", {"key": session.key}), events)

    def test_dismiss_rejects_a_live_session(self):
        c, daemon = make_client_with_daemon()
        session = self._add_terminal_session(daemon)
        headers = {"X-Huginn-Token": "secret-token"}

        r = c.post("/api/sessions/codex%3Athread-1/dismiss", headers=headers)

        self.assertEqual(r.status_code, 409)
        self.assertIn(session.key, daemon.reducer.sessions)

    def test_dismiss_missing_session_is_404(self):
        c = make_client()
        headers = {"X-Huginn-Token": "secret-token"}

        r = c.post("/api/sessions/codex%3Anonexistent/dismiss", headers=headers)

        self.assertEqual(r.status_code, 404)

    def test_steering_preview_fails_closed_for_observe_only_session(self):
        c, daemon = make_client_with_daemon()
        self._add_terminal_session(daemon)
        headers = {"X-Huginn-Token": "secret-token"}

        with patch("huginn.steering.authority_for", return_value="observe"):
            r = c.post(
                "/api/sessions/codex%3Athread-1/steering/preview",
                json={"action": "send", "instruction": "continue"},
                headers=headers,
            )

        self.assertEqual(r.status_code, 403)

    def test_authority_update_is_explicit_and_session_scoped(self):
        c, daemon = make_client_with_daemon()
        session = self._add_terminal_session(daemon)
        headers = {"X-Huginn-Token": "secret-token"}
        with patch(
            "huginn.server.app.set_authority",
            return_value={"key": session.key, "session_id": session.session_id, "level": "steer"},
        ) as update:
            r = c.put(
                "/api/sessions/codex%3Athread-1/authority",
                json={"level": "steer"},
                headers=headers,
            )

        self.assertEqual(r.status_code, 200)
        update.assert_called_once_with(session, "steer")

    def test_cancelled_steering_confirmation_is_one_use(self):
        c, daemon = make_client_with_daemon()
        self._add_terminal_session(daemon)
        headers = {"X-Huginn-Token": "secret-token"}
        with patch("huginn.steering.authority_for", return_value="steer"), \
             patch("huginn.steering._terminal_target", return_value=(None, "ttys001")):
            preview = c.post(
                "/api/sessions/codex%3Athread-1/steering/preview",
                json={"action": "send", "instruction": "  exact input  "},
                headers=headers,
            )
        confirmation_id = preview.json()["confirmation_id"]

        cancelled = c.post(
            "/api/steering/confirm",
            json={"confirmation_id": confirmation_id, "confirmed": False},
            headers=headers,
        )
        repeated = c.post(
            "/api/steering/confirm",
            json={"confirmation_id": confirmation_id, "confirmed": True},
            headers=headers,
        )

        self.assertEqual(cancelled.json(), {"ok": False, "cancelled": True})
        self.assertEqual(repeated.status_code, 422)

    def test_confirmed_steering_executes_the_previewed_action(self):
        c, daemon = make_client_with_daemon()
        session = self._add_terminal_session(daemon)
        headers = {"X-Huginn-Token": "secret-token"}
        with patch("huginn.steering.authority_for", return_value="steer"), \
             patch("huginn.steering._terminal_target", return_value=(None, "ttys001")):
            preview = c.post(
                "/api/sessions/codex%3Athread-1/steering/preview",
                json={"action": "interrupt"},
                headers=headers,
            ).json()
        with patch(
            "huginn.server.app.execute_pending",
            return_value={"ok": True, "action": "interrupt"},
        ) as execute:
            confirmed = c.post(
                "/api/steering/confirm",
                json={"confirmation_id": preview["confirmation_id"], "confirmed": True},
                headers=headers,
            )

        self.assertEqual(confirmed.status_code, 200)
        self.assertEqual(confirmed.json()["action"], "interrupt")
        self.assertIs(execute.call_args.args[1], session)

    def _isolated_config_dir(self):
        """PUT /api/settings calls config.save(), which writes to the real
        ~/.config/huginn/config.toml unless redirected -- isolate it."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patcher1 = patch.object(config_module, "CONFIG_DIR", Path(tmp.name))
        patcher2 = patch.object(config_module, "CONFIG_PATH", Path(tmp.name) / "config.toml")
        patcher1.start(); patcher2.start()
        self.addCleanup(patcher1.stop)
        self.addCleanup(patcher2.stop)

    def test_settings_put_accepts_valid_update(self):
        self._isolated_config_dir()
        c = make_client()
        headers = {"X-Huginn-Token": "secret-token"}
        r = c.put("/api/settings", json={"ui": {"ended_ttl_s": 600}}, headers=headers)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["ui"]["ended_ttl_s"], 600)

    def test_providers_reports_installed_and_missing_options(self):
        class FakeProvider:
            def __init__(self, reason):
                self.reason = reason

            def available(self):
                return self.reason

        c = make_client()
        providers = {
            "claude": FakeProvider(None),
            "codex": FakeProvider("embedded codex binary not found"),
        }
        with patch("huginn.llm.providers.all_providers", return_value=providers):
            r = c.get("/api/providers", headers={"X-Huginn-Token": "secret-token"})
        self.assertEqual(r.status_code, 200)
        body = r.json()["providers"]
        self.assertTrue(body["claude"]["available"])
        self.assertFalse(body["codex"]["available"])
        self.assertEqual(body["codex"]["reason"], "embedded codex binary not found")

    def test_plugins_reports_the_daemon_registry(self):
        c, daemon = make_client_with_daemon()
        daemon.plugins = type("Registry", (), {
            "to_dict": lambda self: {
                "api_version": 1,
                "plugins": [{"name": "example", "version": "1"}],
                "errors": [],
            },
        })()

        r = c.get("/api/plugins", headers={"X-Huginn-Token": "secret-token"})

        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["plugins"][0]["name"], "example")

    def test_settings_put_rejects_invalid_value_with_422(self):
        self._isolated_config_dir()
        c = make_client()
        headers = {"X-Huginn-Token": "secret-token"}
        r = c.put("/api/settings", json={"ui": {"ended_ttl_s": -5}}, headers=headers)
        self.assertEqual(r.status_code, 422)
        # rejected value must not have been applied
        got = c.get("/api/settings", headers=headers).json()
        self.assertNotEqual(got["ui"]["ended_ttl_s"], -5)

    def test_settings_put_rejects_whole_batch_if_any_key_invalid(self):
        self._isolated_config_dir()
        c = make_client()
        headers = {"X-Huginn-Token": "secret-token"}
        r = c.put("/api/settings", json={
            "ui": {"ended_ttl_s": 900},          # valid on its own
            "server": {"port": 999999},           # invalid
        }, headers=headers)
        self.assertEqual(r.status_code, 422)
        got = c.get("/api/settings", headers=headers).json()
        self.assertNotEqual(got["ui"]["ended_ttl_s"], 900, "valid key leaked through a rejected batch")

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

    def test_health_requires_token(self):
        c = make_client()
        self.assertEqual(c.get("/api/health").status_code, 401)

    def test_health_reflects_injected_failure_and_redacts_message(self):
        c, daemon = make_client_with_daemon()
        secret = "leaked prompt text: do not expose me"
        daemon.diagnostics.error("blurb", RuntimeError(secret))
        r = c.get("/api/health", headers={"X-Huginn-Token": "secret-token"})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["sources"]["blurb"]["last_error_class"], "RuntimeError")
        self.assertEqual(body["sources"]["blurb"]["error_count"], 1)
        self.assertNotIn(secret, r.text)

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
