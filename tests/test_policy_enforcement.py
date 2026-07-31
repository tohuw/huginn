"""Every core LLM call routes through the policy chokepoint (issue #41).

test_policy.py pins the resolver's own semantics. This file pins the thing
that makes those semantics matter: that Ask, automatic text, and the provider
listing all consult the chokepoint, and that a refused model is *refused* --
never quietly swapped for a permitted one.
"""
from __future__ import annotations

import time
import unittest
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from huginn.config import Config
from huginn.daemon import Daemon
from huginn.llm.chat import _apply_controls, start_chat
from huginn.model import Session, SessionState
from huginn.policy import ModelPolicy
from huginn.server.app import create_app

BEDROCK_ONLY = ModelPolicy(
    name="bedrock-only",
    allow=(r"^us\.anthropic\.",),
    require_provider="bedrock",
    reason="POLICY_REASON_TOKEN: inference must route through the approved provider",
)


def _installed(*policies: ModelPolicy):
    points = []
    for policy in policies:
        point = Mock()
        point.name = policy.name
        point.load = Mock(return_value=policy)
        points.append(point)
    return patch("huginn.policy.entry_points", return_value=points)


def _no_policy():
    return patch("huginn.policy.entry_points", return_value=[])


class _Provider:
    name = "claude"
    default_blurb_model = "haiku"

    def __init__(self):
        self.calls = 0

    def available(self):
        return None

    async def run_text(self, *args, **kwargs):
        self.calls += 1
        return "automatic text"

    async def stream(self, *args, **kwargs):
        self.calls += 1
        yield "answer"


class ChatChokepointTests(unittest.IsolatedAsyncioTestCase):
    async def test_refused_provider_rejects_the_request_with_the_reason(self):
        daemon = Daemon(Config({}))
        provider = _Provider()

        with _installed(BEDROCK_ONLY), \
             patch("huginn.llm.chat.get_provider", return_value=provider):
            result = await start_chat(daemon, {"question": "what's blocked?"})

        self.assertFalse(result["ok"])
        self.assertIn("POLICY_REASON_TOKEN", result["error"])
        self.assertEqual(provider.calls, 0, "refused model still reached the provider")
        self.assertIsNone(daemon.active_chat)

    async def test_a_body_supplied_provider_cannot_widen_the_allowed_set(self):
        # The POST body naming a provider is a caller narrowing its own choice.
        # It must never be a way around an installed policy.
        daemon = Daemon(Config({}))
        provider = _Provider()

        with _installed(BEDROCK_ONLY), \
             patch("huginn.llm.chat.get_provider", return_value=provider):
            result = await start_chat(
                daemon, {"question": "what's blocked?", "provider": "claude"})

        self.assertFalse(result["ok"])
        self.assertIn("POLICY_REASON_TOKEN", result["error"])
        self.assertEqual(provider.calls, 0)

    async def test_permitted_pair_runs_normally_under_a_policy(self):
        daemon = Daemon(Config({"llm": {
            "provider": "bedrock", "chat_model": "us.anthropic.claude-sonnet-5"}}))
        provider = _Provider()
        provider.name = "bedrock"
        daemon.reducer.sessions["claude:1"] = Session(
            key="claude:1", source="claude", session_id="s1", cwd="/tmp",
            name="work", state=SessionState.WORKING, state_since=time.time(),
        )

        with _installed(BEDROCK_ONLY), \
             patch("huginn.llm.chat.get_provider", return_value=provider), \
             patch("huginn.llm.chat.compatible_model",
                   return_value="us.anthropic.claude-sonnet-5"):
            result = await start_chat(daemon, {"question": "what's blocked?"})
            self.assertTrue(result["ok"])
            await daemon.active_chat

        self.assertEqual(provider.calls, 1)

    async def test_unrestricted_default_is_unchanged(self):
        daemon = Daemon(Config({}))
        provider = _Provider()

        with _no_policy(), patch("huginn.llm.chat.get_provider", return_value=provider):
            result = await start_chat(daemon, {"question": "what's blocked?"})
            self.assertTrue(result["ok"])
            await daemon.active_chat

    async def test_ask_control_cannot_switch_to_a_forbidden_provider(self):
        # "use codex" in a chat sentence is still a config write, so it faces
        # the same gate PUT /api/settings does.
        daemon = Daemon(Config({"llm": {"provider": "bedrock"}}))
        daemon.bus.broadcast = lambda event, data: None

        with _installed(BEDROCK_ONLY), patch("huginn.config.save"):
            replies = _apply_controls(
                daemon, [("llm", "provider", "codex", "Ask agent set to codex.")])

        self.assertIn("POLICY_REASON_TOKEN", " ".join(replies))
        self.assertEqual(daemon.cfg.get("llm", "provider"), "bedrock")


class BlurbChokepointTests(unittest.IsolatedAsyncioTestCase):
    async def test_refused_model_never_reaches_the_provider(self):
        daemon = Daemon(Config({"llm": {
            "enabled": True, "blurb_debounce_s": 0, "provider": "claude"}}))
        provider = _Provider()

        with _installed(BEDROCK_ONLY), \
             patch("huginn.llm.blurb.get_provider", return_value=provider):
            self.assertIsNone(await daemon.blurbs._run_prompt("summarize this"))

        self.assertEqual(provider.calls, 0)
        # Pin *why* it returned None: a budget or availability short-circuit
        # would also produce None, and would pass a weaker assertion.
        self.assertEqual(daemon.diagnostics.snapshot()["blurb"]["last_error_class"],
                         "PolicyRefused")

    async def test_refusal_latches_the_circuit_permanently(self):
        # PolicyRefused carries retryable=False, so the circuit must not keep
        # re-attempting a refused model once a minute for every session.
        daemon = Daemon(Config({"llm": {
            "enabled": True, "blurb_debounce_s": 0, "provider": "claude"}}))
        provider = _Provider()

        with _installed(BEDROCK_ONLY), \
             patch("huginn.llm.blurb.get_provider", return_value=provider):
            await daemon.blurbs._run_prompt("summarize this")

        self.assertTrue(daemon.blurbs.status()["circuit"]["permanent"])


class ProvidersEndpointTests(unittest.TestCase):
    def _client(self) -> tuple[TestClient, Daemon]:
        daemon = Daemon(Config({}))
        daemon.token = "secret-token"
        daemon.refresh_token = "refresh"
        return TestClient(create_app(daemon), base_url="http://127.0.0.1"), daemon

    def test_forbidden_provider_is_reported_unavailable_with_the_reason(self):
        client, _ = self._client()

        with _installed(BEDROCK_ONLY):
            body = client.get("/api/providers",
                              headers={"X-Huginn-Token": "secret-token"}).json()["providers"]

        self.assertFalse(body["claude"]["available"])
        self.assertIn("POLICY_REASON_TOKEN", body["claude"]["reason"])

    def test_a_refused_automatic_model_is_reported_as_none_not_substituted(self):
        client, _ = self._client()
        permitted_provider_only = ModelPolicy(
            name="model-locked", allow=(r"^us\.anthropic\.",),
            reason="only us.anthropic.* ids")

        with _installed(permitted_provider_only):
            body = client.get("/api/providers",
                              headers={"X-Huginn-Token": "secret-token"}).json()["providers"]

        # The provider itself is not forbidden (no require_provider), but its
        # resolved automatic model "haiku" is -- and must not be swapped out.
        self.assertIsNone(body["claude"]["automatic_model"])

    def test_no_policy_leaves_the_listing_exactly_as_before(self):
        client, _ = self._client()

        with _no_policy():
            body = client.get("/api/providers",
                              headers={"X-Huginn-Token": "secret-token"}).json()["providers"]

        self.assertIn("claude", body)
        self.assertIn("codex", body)
        for entry in body.values():
            self.assertNotIn("refused", str(entry["reason"]))

    def test_settings_put_refuses_a_policy_forbidden_provider_with_422(self):
        client, _ = self._client()

        with _installed(BEDROCK_ONLY):
            response = client.put("/api/settings", json={"llm": {"provider": "codex"}},
                                  headers={"X-Huginn-Token": "secret-token"})

        self.assertEqual(response.status_code, 422)
        self.assertIn("POLICY_REASON_TOKEN", str(response.json()))


if __name__ == "__main__":
    unittest.main()
