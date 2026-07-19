"""Chat request-ID correlation across SSE subscribers, and per-daemon (not
module-global) chat state -- issue #17."""
from __future__ import annotations

import asyncio
import contextlib
import unittest
from unittest.mock import patch

from huginn.config import Config
from huginn.daemon import Daemon
from huginn.llm.chat import _control_actions, start_chat
from huginn.model import Session


class _AvailableProvider:
    def available(self):
        return None

    async def stream(self, *args, **kwargs):
        yield "answer"


class _UnavailableProvider:
    def available(self):
        return "not configured"


class _CapturingProvider(_AvailableProvider):
    def __init__(self):
        self.prompt = ""

    async def stream(self, prompt, **kwargs):
        self.prompt = prompt
        yield "redirect"


class ChatCorrelationTests(unittest.IsolatedAsyncioTestCase):
    async def test_request_id_is_returned_and_tags_every_broadcast(self):
        daemon = Daemon(Config({}))
        events = []
        daemon.bus.broadcast = lambda event, data: events.append((event, data))

        with patch("huginn.llm.chat.get_provider", return_value=_AvailableProvider()):
            result = await start_chat(daemon, {"question": "how's it going?"})
        self.assertTrue(result["ok"])
        request_id = result["request_id"]
        self.assertTrue(request_id)

        await daemon.active_chat
        self.assertTrue(events)
        for _event, data in events:
            self.assertEqual(data["request_id"], request_id)

    async def test_second_request_rejected_while_one_is_running(self):
        daemon = Daemon(Config({}))
        daemon.bus.broadcast = lambda *a, **k: None

        class _SlowProvider(_AvailableProvider):
            async def stream(self, *args, **kwargs):
                await asyncio.sleep(10)
                yield "never"   # pragma: no cover

        with patch("huginn.llm.chat.get_provider", return_value=_SlowProvider()):
            first = await start_chat(daemon, {"question": "first?"})
            second = await start_chat(daemon, {"question": "second?"})
        self.assertTrue(first["ok"])
        self.assertFalse(second["ok"])
        self.assertIn("already running", second["error"])
        daemon.active_chat.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await daemon.active_chat

    async def test_two_daemon_instances_do_not_share_active_chat(self):
        d1, d2 = Daemon(Config({})), Daemon(Config({}))
        d1.bus.broadcast = lambda *a, **k: None
        d2.bus.broadcast = lambda *a, **k: None

        class _SlowProvider(_AvailableProvider):
            async def stream(self, *args, **kwargs):
                await asyncio.sleep(10)
                yield "never"   # pragma: no cover

        with patch("huginn.llm.chat.get_provider", return_value=_SlowProvider()):
            r1 = await start_chat(d1, {"question": "d1 question"})
            r2 = await start_chat(d2, {"question": "d2 question"})
        self.assertTrue(r1["ok"])
        self.assertTrue(r2["ok"], "second daemon's chat was rejected -- state is shared")
        self.assertIsNot(d1.active_chat, d2.active_chat)
        d1.active_chat.cancel()
        d2.active_chat.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await d1.active_chat
        with contextlib.suppress(asyncio.CancelledError):
            await d2.active_chat

    async def test_unavailable_provider_rejected_without_request_id(self):
        daemon = Daemon(Config({}))
        with patch("huginn.llm.chat.get_provider", return_value=_UnavailableProvider()):
            result = await start_chat(daemon, {"question": "hi"})
        self.assertFalse(result["ok"])
        self.assertNotIn("request_id", result)

    async def test_ask_can_set_a_manual_card_title(self):
        daemon = Daemon(Config({}))
        daemon.bus.broadcast = lambda *a, **k: None
        session = Session(key="codex:test", source="codex", session_id="test",
                          cwd="/tmp", name="test-agent")
        daemon.reducer.sessions[session.key] = session
        result = await start_chat(daemon, {"question": "title @test-agent release cleanup"})
        self.assertTrue(result["ok"])
        await daemon.active_chat
        self.assertEqual(session.title, "release cleanup")
        self.assertEqual(session.title_origin, "manual")

    async def test_prompt_restricts_ask_agent_to_session_questions(self):
        daemon = Daemon(Config({}))
        daemon.bus.broadcast = lambda *a, **k: None
        daemon.reducer.sessions["codex:test"] = Session(
            key="codex:test", source="codex", session_id="test",
            cwd="/tmp", name="test-agent")
        provider = _CapturingProvider()
        with patch("huginn.llm.chat.get_provider", return_value=provider):
            result = await start_chat(daemon, {"question": "How do I make lasagna?"})
        self.assertTrue(result["ok"])
        await daemon.active_chat
        self.assertIn("scope is exclusively the agent sessions", provider.prompt)
        self.assertIn("Do not provide a recipe", provider.prompt)
        self.assertIn("Question: How do I make lasagna?", provider.prompt)

    def test_dashboard_control_commands_are_recognized(self):
        cases = {
            "disable blurbs": ("llm", "enabled", False),
            "switch the agent preference to codex": ("llm", "provider", "codex"),
            "show list view": ("ui", "view", "list"),
            "switch back to cards": ("ui", "view", "cards"),
            "hide the ask panel": ("ui", "chat_open", False),
            "hide desktop presence": ("ui", "show_desktop", False),
            "show app tiles": ("ui", "show_desktop", True),
            "span ask horizontally": ("ui", "chat_span", "horizontal"),
            "dock ask on the right": ("ui", "chat_span", "vertical"),
            "sort by state": ("ui", "sort", "state"),
            "sort alphabetically": ("ui", "sort", "alpha"),
            "sort newest first": ("ui", "sort", "newest"),
            "order by oldest": ("ui", "sort", "oldest"),
        }
        for question, expected in cases.items():
            actions = _control_actions(question)
            self.assertIn(expected, [a[:3] for a in actions], msg=question)

    async def test_dashboard_control_updates_settings_and_broadcasts(self):
        daemon = Daemon(Config({}))
        events = []
        daemon.bus.broadcast = lambda event, data: events.append((event, data))
        with patch("huginn.llm.chat.config.save"):
            result = await start_chat(
                daemon, {"question": "switch to list view and hide the ask panel"})
            self.assertTrue(result["ok"])
            self.assertEqual(result["settings"]["ui"]["view"], "list")
            self.assertFalse(result["settings"]["ui"]["chat_open"])
            await daemon.active_chat
        self.assertEqual(daemon.cfg.get("ui", "view"), "list")
        self.assertFalse(daemon.cfg.get("ui", "chat_open"))
        self.assertTrue(any(event == "settings.changed" for event, _ in events))


if __name__ == "__main__":
    unittest.main()
