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
from huginn.model import Session, SessionState


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

    async def test_a_non_latin1_blurb_does_not_break_ask(self):
        """Ask hung forever on Windows whenever any session's text held "→".

        The per-session digests were written with Path.write_text and no
        encoding, so Python used the *locale* encoding -- cp1252 there. One
        arrow or em dash raised UnicodeEncodeError, the wrapper turned it into
        a chat.error, and the panel rendered that as nothing: an endless
        throbber, on every provider, for as long as that text was on the roster.

        Asserted through start_chat rather than by unit-testing the write, so a
        future refactor that reintroduces a locale-encoded path still fails.
        """
        daemon = Daemon(Config({}))
        events = []
        daemon.bus.broadcast = lambda event, data: events.append((event, data))
        daemon.reducer.sessions["claude:1"] = Session(
            key="claude:1", source="claude", session_id="s1",
            cwd="/tmp/repo", name="arrows", state=SessionState.WORKING,
            state_since=0.0,
            # Every one of these is outside cp1252.
            blurb="fixed → verified · 100% ✓ — shipped",
            title="macOS 481s → 34s",
        )

        with patch("huginn.llm.chat.get_provider", return_value=_AvailableProvider()):
            result = await start_chat(daemon, {"question": "what is running?"})
        self.assertTrue(result["ok"], result)
        await daemon.active_chat

        errors = [data for event, data in events if event == "chat.error"]
        self.assertEqual(errors, [], f"Ask failed instead of answering: {errors}")
        self.assertIn("chat.done", [event for event, _ in events])

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

    async def test_ask_jump_focuses_the_named_session(self):
        daemon = Daemon(Config({}))
        events = []
        daemon.bus.broadcast = lambda event, data: events.append((event, data))
        session = Session(key="codex:test", source="codex", session_id="test",
                          cwd="/tmp", name="test-agent")
        daemon.reducer.sessions[session.key] = session
        with patch("huginn.focus.focus_session", return_value={"ok": True, "target": "iTerm2"}) as mock_focus:
            result = await start_chat(daemon, {"question": "jump @test-agent"})
        self.assertTrue(result["ok"])
        await daemon.active_chat
        mock_focus.assert_called_once_with(session)
        self.assertTrue(any(event == "session.focused" and data["key"] == session.key
                             for event, data in events))

    async def test_ask_jump_reports_focus_failure(self):
        daemon = Daemon(Config({}))
        daemon.bus.broadcast = lambda *a, **k: None
        session = Session(key="codex:test", source="codex", session_id="test",
                          cwd="/tmp", name="test-agent")
        daemon.reducer.sessions[session.key] = session
        answers = []
        with patch("huginn.focus.focus_session", return_value={"ok": False, "error": "no tty"}), \
             patch("huginn.llm.chat.get_provider") as get_provider:
            daemon.bus.broadcast = lambda event, data: answers.append((event, data))
            result = await start_chat(daemon, {"question": "jump @test-agent"})
            await daemon.active_chat
        get_provider.assert_not_called()
        self.assertTrue(result["ok"])
        self.assertTrue(any("no tty" in data.get("text", "") for _e, data in answers))

    async def test_ask_peek_shows_and_returns_the_transcript_tail(self):
        daemon = Daemon(Config({}))
        events = []
        daemon.bus.broadcast = lambda event, data: events.append((event, data))
        session = Session(key="codex:test", source="codex", session_id="test",
                          cwd="/tmp", name="test-agent")
        daemon.reducer.sessions[session.key] = session
        with patch("huginn.llm.chat.evidence_for_session", return_value=["assistant: done"]):
            result = await start_chat(daemon, {"question": "peek @test-agent"})
        self.assertTrue(result["ok"])
        await daemon.active_chat
        peeks = [data for event, data in events if event == "session.peek"]
        self.assertEqual(peeks, [{"key": session.key, "lines": ["assistant: done"]}])
        self.assertTrue(any("assistant: done" in data.get("text", "")
                             for event, data in events if event == "chat.delta"))

    async def test_jump_and_peek_report_ambiguous_names(self):
        daemon = Daemon(Config({}))
        events = []
        daemon.bus.broadcast = lambda event, data: events.append((event, data))
        daemon.reducer.sessions["codex:a"] = Session(
            key="codex:a", source="codex", session_id="a", cwd="/tmp", name="dup-one")
        daemon.reducer.sessions["codex:b"] = Session(
            key="codex:b", source="codex", session_id="b", cwd="/tmp", name="dup-two")
        result = await start_chat(daemon, {"question": "jump @dup"})
        self.assertTrue(result["ok"])
        await daemon.active_chat
        self.assertTrue(any("uniquely match" in data.get("text", "") for _e, data in events))

    async def test_roster_includes_title_or_blurb_for_open_ended_search(self):
        daemon = Daemon(Config({}))
        daemon.bus.broadcast = lambda *a, **k: None
        titled = Session(key="codex:titled", source="codex", session_id="titled",
                          cwd="/tmp", name="titled-agent", title="release cleanup")
        blurbed = Session(key="codex:blurbed", source="codex", session_id="blurbed",
                           cwd="/tmp", name="blurbed-agent", blurb="Debugging flaky test")
        daemon.reducer.sessions[titled.key] = titled
        daemon.reducer.sessions[blurbed.key] = blurbed
        provider = _CapturingProvider()
        with patch("huginn.llm.chat.get_provider", return_value=provider):
            result = await start_chat(daemon, {"question": "which session is about flaky tests?"})
        self.assertTrue(result["ok"])
        await daemon.active_chat
        self.assertIn('"release cleanup"', provider.prompt)
        self.assertIn('"Debugging flaky test"', provider.prompt)

    async def test_ask_can_dismiss_an_ended_session(self):
        daemon = Daemon(Config({}))
        events = []
        daemon.bus.broadcast = lambda event, data: events.append((event, data))
        session = Session(key="codex:test", source="codex", session_id="test",
                          cwd="/tmp", name="test-agent", state=SessionState.ENDED)
        daemon.reducer.sessions[session.key] = session
        result = await start_chat(daemon, {"question": "dismiss @test-agent"})
        self.assertTrue(result["ok"])
        await daemon.active_chat
        self.assertNotIn(session.key, daemon.reducer.sessions)
        self.assertIn(("session.remove", {"key": session.key}), events)

    async def test_ask_refuses_to_dismiss_a_live_session(self):
        daemon = Daemon(Config({}))
        daemon.bus.broadcast = lambda *a, **k: None
        session = Session(key="codex:test", source="codex", session_id="test",
                          cwd="/tmp", name="test-agent")
        daemon.reducer.sessions[session.key] = session
        result = await start_chat(daemon, {"question": "dismiss @test-agent"})
        self.assertTrue(result["ok"])
        await daemon.active_chat
        self.assertIn(session.key, daemon.reducer.sessions)

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
