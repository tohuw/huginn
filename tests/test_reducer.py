"""Reducer transition rules + parser tolerance, against realistic payloads."""
import time
import unittest

from huginn.config import Config
from huginn.model import Event, Session, SessionState
from huginn.state import Reducer
from huginn.sources.transcript import ClaudeAnalyzer, CodexAnalyzer

NOW = time.time()


def claude_session(key="claude:100", state=SessionState.IDLE, **kw):
    defaults = dict(key=key, source="claude", session_id="sid-100",
                    cwd="/tmp/p", name="p-1", pid=100, state=state,
                    state_since=NOW, state_origin="statusfile", last_activity=NOW)
    defaults.update(kw)
    return Session(**defaults)


class ReducerTests(unittest.TestCase):
    def setUp(self):
        self.r = Reducer(Config({}))

    def feed(self, kind, key, payload, ts=None):
        return self.r.apply(Event(kind, key, ts or time.time(), "test", payload))

    def test_new_session_upserts(self):
        changed = self.feed("claude.file", "claude:100",
                            {"session": claude_session()})
        self.assertEqual(len(changed), 1)
        self.assertIn("claude:100", self.r.sessions)

    def test_busy_overrides_stale_waiting(self):
        s = claude_session(state=SessionState.WAITING_INPUT,
                           state_origin="hook", state_since=NOW - 60)
        self.r.sessions[s.key] = s
        self.feed("claude.file", s.key,
                  {"session": claude_session(state=SessionState.WORKING)})
        self.assertEqual(s.state, SessionState.WORKING)

    def test_hook_grace_blocks_statusfile(self):
        s = claude_session(state=SessionState.WAITING_PERMISSION,
                           state_origin="hook", state_since=time.time())
        self.r.sessions[s.key] = s
        self.feed("claude.file", s.key,
                  {"session": claude_session(state=SessionState.WORKING)})
        self.assertEqual(s.state, SessionState.WAITING_PERMISSION)

    def test_notification_classification(self):
        s = claude_session(state=SessionState.WORKING)
        self.r.sessions[s.key] = s
        self.feed("hook.claude", None, {
            "event": "Notification",
            "data": {"session_id": "sid-100",
                     "message": "Claude needs your permission to use Bash"}})
        self.assertEqual(s.state, SessionState.WAITING_PERMISSION)
        self.feed("hook.claude", None, {
            "event": "Notification",
            "data": {"session_id": "sid-100",
                     "message": "Claude is waiting for your input"}})
        self.assertEqual(s.state, SessionState.WAITING_INPUT)

    def test_stop_done_vs_question(self):
        s = claude_session(state=SessionState.WORKING)
        self.r.sessions[s.key] = s
        self.feed("hook.claude", None, {
            "event": "Stop", "data": {"session_id": "sid-100"},
            "asked_question": True})
        self.assertEqual(s.state, SessionState.WAITING_INPUT)
        s.state_since = NOW - 60
        self.feed("hook.claude", None, {
            "event": "Stop", "data": {"session_id": "sid-100"}})
        self.assertEqual(s.state, SessionState.DONE)

    def test_dead_while_working_is_error(self):
        s = claude_session(state=SessionState.WORKING)
        self.r.sessions[s.key] = s
        self.feed("claude.dead", s.key, {})
        self.assertEqual(s.state, SessionState.ERROR)

    def test_pending_tool_timeout(self):
        s = claude_session(state=SessionState.IDLE)
        self.r.sessions[s.key] = s
        self.feed("tick", None, {"pending_ages": {s.key: 25.0}})
        self.assertEqual(s.state, SessionState.WAITING_PERMISSION)

    def test_ended_ttl_removal(self):
        s = claude_session(state=SessionState.ENDED,
                           state_origin="timeout", state_since=NOW - 400)
        self.r.sessions[s.key] = s
        self.feed("tick", None, {"pending_ages": {}})
        self.assertNotIn(s.key, self.r.sessions)
        self.assertEqual(self.r.removed, [s.key])


class ClaudeAnalyzerTests(unittest.TestCase):
    def test_pending_tools_and_question(self):
        an = ClaudeAnalyzer()
        an.feed([
            {"type": "user", "message": {"content": [{"type": "text", "text": "do a thing"}]}},
            {"type": "assistant", "message": {"model": "m", "content": [
                {"type": "tool_use", "id": "t1", "name": "Bash", "input": {}}]}},
        ])
        self.assertEqual(len(an.pending_tools), 1)
        an.feed([{"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "t1"}]}}])
        self.assertEqual(len(an.pending_tools), 0)
        an.feed([{"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "t2", "name": "AskUserQuestion", "input": {}}]}}])
        self.assertTrue(an.asked_user_question)
        self.assertEqual(an.last_prompt, "do a thing")

    def test_error_detection(self):
        an = ClaudeAnalyzer()
        an.feed([{"type": "system", "isApiErrorMessage": True}])
        self.assertTrue(an.error)

    def test_tolerates_unknown_lines(self):
        an = ClaudeAnalyzer()
        self.assertFalse(an.feed([{"type": "file-history-snapshot"},
                                  {"type": "ai-title"}, {"unknown": 1}]))


class CodexAnalyzerTests(unittest.TestCase):
    def test_turn_lifecycle(self):
        an = CodexAnalyzer()
        an.feed([
            {"type": "event_msg", "payload": {"type": "user_message", "message": "hi"}},
            {"type": "event_msg", "payload": {"type": "task_started"}},
        ])
        self.assertEqual(an.phase, "working")
        an.feed([{"type": "event_msg", "payload": {"type": "agent_message", "message": "done!"}},
                 {"type": "event_msg", "payload": {"type": "task_complete"}}])
        self.assertEqual(an.phase, "done")
        self.assertEqual(an.last_prompt, "hi")


if __name__ == "__main__":
    unittest.main()
