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

    def test_app_activity_never_counts_as_attention(self):
        tile = claude_session(
            key="claude-desktop", state=SessionState.ACTIVE,
            source="claude-desktop", pid=None,
        )
        self.r.sessions[tile.key] = tile
        self.assertEqual(self.r.attention_count(), 0)

    def test_busy_overrides_stale_waiting(self):
        s = claude_session(state=SessionState.WAITING_INPUT,
                           state_origin="hook", state_since=NOW - 60,
                           blurb="Needs permission to push", blurb_ts=NOW - 59)
        self.r.sessions[s.key] = s
        self.feed("claude.file", s.key,
                  {"session": claude_session(state=SessionState.WORKING)})
        self.assertEqual(s.state, SessionState.WORKING)
        self.assertIsNone(s.blurb)

    def test_restored_stale_blurb_is_cleared_without_state_change(self):
        s = claude_session(state=SessionState.WORKING,
                           blurb="Needs permission", blurb_ts=NOW - 60,
                           state_since=NOW - 30)
        self.r.sessions[s.key] = s
        self.feed("claude.file", s.key,
                  {"session": claude_session(state=SessionState.WORKING)})
        self.assertIsNone(s.blurb)

    def test_hook_grace_blocks_statusfile(self):
        s = claude_session(state=SessionState.WAITING_PERMISSION,
                           state_origin="hook", state_since=time.time())
        self.r.sessions[s.key] = s
        self.feed("claude.file", s.key,
                  {"session": claude_session(state=SessionState.WORKING)})
        self.assertEqual(s.state, SessionState.WAITING_PERMISSION)

    def test_background_shell_does_not_override_completed_turn(self):
        s = claude_session(state=SessionState.WORKING, shells=2)
        self.r.sessions[s.key] = s
        self.feed("claude.file", s.key, {
            "session": claude_session(state=SessionState.WORKING, shells=2),
            "recent_turn_end": True,
        })
        self.assertEqual(s.state, SessionState.DONE)
        self.assertEqual(s.shells, 2)

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

    def test_notification_classification_more_patterns(self):
        # More representative messages, pinning the rest of the default
        # [patterns] lists (issue #1 -- no real-traffic corpus exists yet to
        # tune these against, so this is a regression net, not tuned data).
        cases = [
            ("Do you approve this action?", SessionState.WAITING_PERMISSION),
            ("This requires authorization before continuing", SessionState.WAITING_PERMISSION),
            ("Bash command not allowed without approval", SessionState.WAITING_PERMISSION),
            ("Session is idle", SessionState.WAITING_INPUT),
        ]
        for message, expected in cases:
            r = Reducer(Config({}))
            s = claude_session(state=SessionState.WORKING)
            r.sessions[s.key] = s
            r.apply(Event("hook.claude", None, time.time(), "test",
                          {"event": "Notification",
                           "data": {"session_id": "sid-100", "message": message}}))
            self.assertEqual(s.state, expected, msg=message)

    def test_notification_type_takes_priority_over_message(self):
        cases = [
            ("permission_prompt", "Claude is idle", SessionState.WAITING_PERMISSION),
            ("idle_prompt", "permission granted", SessionState.DONE),
            ("elicitation_dialog", "", SessionState.WAITING_INPUT),
        ]
        for notification_type, message, expected in cases:
            r = Reducer(Config({}))
            s = claude_session(state=SessionState.WORKING)
            r.sessions[s.key] = s
            r.apply(Event("hook.claude", None, time.time(), "test", {
                "event": "Notification",
                "data": {"session_id": "sid-100", "message": message,
                         "notification_type": notification_type},
            }))
            self.assertEqual(s.state, expected, msg=notification_type)

    def test_ask_user_question_notification_is_waiting_input(self):
        # AskUserQuestion arrives as a permission-shaped Notification; the
        # hook endpoint attaches asked_question from the transcript tail and
        # the reducer must classify it as WAITING_INPUT, not
        # WAITING_PERMISSION (the "PERMIT?" badge bug).
        for data in (
                {"session_id": "sid-100", "message": "",
                 "notification_type": "permission_prompt"},
                {"session_id": "sid-100",
                 "message": "Claude needs your permission to use AskUserQuestion"},
        ):
            r = Reducer(Config({}))
            s = claude_session(state=SessionState.WORKING)
            r.sessions[s.key] = s
            r.apply(Event("hook.claude", None, time.time(), "test", {
                "event": "Notification", "data": data,
                "asked_question": True,
            }))
            self.assertEqual(s.state, SessionState.WAITING_INPUT, msg=data)

    def test_real_permission_prompt_unaffected_by_asked_question_false(self):
        s = claude_session(state=SessionState.WORKING)
        self.r.sessions[s.key] = s
        self.feed("hook.claude", None, {
            "event": "Notification",
            "data": {"session_id": "sid-100",
                     "notification_type": "permission_prompt"},
            "asked_question": False,
        })
        self.assertEqual(s.state, SessionState.WAITING_PERMISSION)

    def test_delayed_idle_prompt_clears_false_attention(self):
        r = Reducer(Config({}))
        s = claude_session(state=SessionState.WAITING_INPUT)
        r.sessions[s.key] = s
        self.assertEqual(r.attention_count(), 1)
        r.apply(Event("hook.claude", None, time.time(), "test", {
            "event": "Notification",
            "data": {"session_id": "sid-100", "notification_type": "idle_prompt"},
        }))
        self.assertEqual(s.state, SessionState.DONE)
        self.assertEqual(r.attention_count(), 0)

    def test_non_attention_notification_types_do_not_change_state(self):
        for notification_type in (
                "auth_success", "elicitation_complete", "elicitation_response"):
            r = Reducer(Config({}))
            s = claude_session(state=SessionState.WORKING)
            r.sessions[s.key] = s
            changed = r.apply(Event("hook.claude", None, time.time(), "test", {
                "event": "Notification",
                "data": {"session_id": "sid-100",
                         "notification_type": notification_type},
            }))
            self.assertEqual(changed, [], msg=notification_type)
            self.assertEqual(s.state, SessionState.WORKING, msg=notification_type)

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

    def test_show_ended_false_removes_ended_sessions_immediately(self):
        # issue #19: ui.show_ended previously had no effect at all.
        r = Reducer(Config({"ui": {"show_ended": False}}))
        s = claude_session(state=SessionState.ENDED, state_origin="timeout", state_since=NOW)
        r.sessions[s.key] = s
        r.apply(Event("tick", None, NOW, "test", {"pending_ages": {}}))
        self.assertNotIn(s.key, r.sessions)
        self.assertEqual(r.removed, [s.key])

    def test_show_ended_true_still_honors_ended_ttl_s(self):
        r = Reducer(Config({"ui": {"show_ended": True, "ended_ttl_s": 300}}))
        s = claude_session(state=SessionState.ENDED, state_origin="timeout", state_since=NOW)
        r.sessions[s.key] = s
        r.apply(Event("tick", None, NOW, "test", {"pending_ages": {}}))
        self.assertIn(s.key, r.sessions, "a freshly-ended session shouldn't vanish before its TTL")

    def test_snapshot_restore_round_trip(self):
        live = claude_session(state=SessionState.WAITING_INPUT, blurb="doing a thing")
        ended = claude_session(key="claude:200", state=SessionState.ENDED)
        self.r.sessions = {live.key: live, ended.key: ended}
        data = self.r.snapshot()
        self.assertIn(live.key, data)
        self.assertNotIn(ended.key, data)   # ENDED sessions aren't worth persisting

        r2 = Reducer(Config({}))
        r2.restore(data)
        restored = r2.sessions[live.key]
        self.assertEqual(restored.state, SessionState.WAITING_INPUT)
        self.assertEqual(restored.blurb, "doing a thing")
        self.assertEqual(restored.pid, live.pid)

    def test_restore_tolerates_garbage(self):
        r2 = Reducer(Config({}))
        r2.restore({"claude:1": {"key": "claude:1"}})   # missing required fields
        self.assertNotIn("claude:1", r2.sessions)


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

    def test_subagent_spawn_and_completion(self):
        # Real shapes captured from a live session (issue #8 research): the
        # spawning tool_use is named "Agent", and completion arrives later as
        # a queue-operation entry whose content carries <task-notification>.
        an = ClaudeAnalyzer()
        an.feed([{"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "toolu_1", "name": "Agent",
             "input": {"description": "do research", "subagent_type": "Explore"}}]}}])
        self.assertEqual(an.activity()["subagents"], {"running": 1})

        an.feed([{"type": "queue-operation", "operation": "enqueue",
                  "content": "<task-notification>\n<task-id>abc123</task-id>\n"
                             "<tool-use-id>toolu_1</tool-use-id>\n"
                             "<status>completed</status>\n"
                             "<summary>done</summary>\n</task-notification>"}])
        self.assertEqual(an.activity()["subagents"], {"done": 1})

    def test_no_subagents_reports_none(self):
        an = ClaudeAnalyzer()
        an.feed([{"type": "user", "message": {"content": [{"type": "text", "text": "hi"}]}}])
        self.assertIsNone(an.activity()["subagents"])


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

    def test_exec_approval_request_sets_waiting_permission(self):
        an = CodexAnalyzer()
        an.feed([{"type": "event_msg",
                  "payload": {"type": "exec_approval_request", "call_id": "1", "command": ["ls"]}}])
        self.assertEqual(an.phase, "waiting_permission")

    def test_apply_patch_approval_request_sets_waiting_permission(self):
        an = CodexAnalyzer()
        an.feed([{"type": "event_msg",
                  "payload": {"type": "apply_patch_approval_request", "call_id": "1"}}])
        self.assertEqual(an.phase, "waiting_permission")

    def test_desktop_protocol_approval_aliases_set_waiting_permission(self):
        for kind in ("command_execution_approval_request", "file_change_approval_request",
                     "permissions_approval_request"):
            with self.subTest(kind=kind):
                an = CodexAnalyzer()
                an.feed([{"type": "event_msg", "payload": {"type": kind, "call_id": "1"}}])
                self.assertEqual(an.phase, "waiting_permission")

    def test_request_user_input_sets_waiting_input(self):
        an = CodexAnalyzer()
        an.feed([{"type": "event_msg",
                  "payload": {"type": "request_user_input", "call_id": "1"}}])
        self.assertEqual(an.phase, "waiting_input")


def codex_session(key="codex:t1", state=SessionState.WORKING, **kw):
    defaults = dict(key=key, source="codex", session_id="t1", cwd="/tmp/p", name="p-1",
                    state=state, state_since=NOW, state_origin="poll", last_activity=NOW)
    defaults.update(kw)
    return Session(**defaults)


class CodexWaitingReducerTests(unittest.TestCase):
    """Issue #3: no approval/question event was ever observed locally when this
    was written, so this pins the reducer wiring against synthetic payloads."""

    def setUp(self):
        self.r = Reducer(Config({}))

    def feed(self, kind, key, payload, ts=None):
        return self.r.apply(Event(kind, key, ts or time.time(), "test", payload))

    def test_approval_request_reaches_waiting_permission(self):
        s = codex_session()
        self.r.sessions[s.key] = s
        an = CodexAnalyzer()
        an.feed([{"type": "event_msg", "payload": {"type": "exec_approval_request"}}])
        self.feed("codex.activity", s.key, an.activity())
        self.assertEqual(s.state, SessionState.WAITING_PERMISSION)

    def test_user_input_request_reaches_waiting_input(self):
        s = codex_session()
        self.r.sessions[s.key] = s
        an = CodexAnalyzer()
        an.feed([{"type": "event_msg", "payload": {"type": "request_user_input"}}])
        self.feed("codex.activity", s.key, an.activity())
        self.assertEqual(s.state, SessionState.WAITING_INPUT)

    def test_missing_from_complete_scan_ends_session(self):
        s = codex_session(state=SessionState.DONE)
        self.r.sessions[s.key] = s
        changed = self.feed("codex.missing", s.key, {})
        self.assertEqual(changed, [])
        self.assertNotIn(s.key, self.r.sessions)
        self.assertEqual(self.r.removed, [s.key])

    def test_poll_corrects_stale_transcript_working_state(self):
        s = codex_session(state=SessionState.WORKING, state_origin="transcript",
                          state_since=NOW - 120, last_activity=NOW - 120)
        self.r.sessions[s.key] = s
        incoming = codex_session(state=SessionState.DONE, state_origin="poll",
                                 state_since=NOW - 60, last_activity=NOW - 60)
        self.feed("codex.thread", s.key, {"session": incoming})
        self.assertEqual(s.state, SessionState.DONE)
        self.assertEqual(s.state_origin, "poll")

    def test_poll_does_not_override_fresh_hook_state(self):
        s = codex_session(state=SessionState.WORKING, state_origin="hook",
                          state_since=NOW)
        self.r.sessions[s.key] = s
        incoming = codex_session(state=SessionState.DONE, state_origin="poll")
        self.feed("codex.thread", s.key, {"session": incoming}, ts=NOW + 1)
        self.assertEqual(s.state, SessionState.WORKING)

    def test_session_hide_removes_idle_source_record(self):
        s = codex_session(state=SessionState.IDLE)
        self.r.sessions[s.key] = s
        self.feed("session.hide", s.key, {})
        self.assertNotIn(s.key, self.r.sessions)
        self.assertEqual(self.r.removed, [s.key])


class CodexHookReconciliationTests(unittest.TestCase):
    """issue #20: Codex hooks were installed and counted (issue #2) but had
    no reducer handler at all -- they never affected state. Payload shapes
    below (session_id/turn_id/cwd) match field names found in the real
    Codex binary's embedded hook-construction strings."""

    def setUp(self):
        self.r = Reducer(Config({}))

    def feed(self, kind, key, payload, ts=None):
        return self.r.apply(Event(kind, key, ts or time.time(), "test", payload))

    def test_unknown_thread_is_a_safe_noop(self):
        # a thread the poller hasn't discovered yet -- must not crash or
        # fabricate a session; discovery stays poll's job.
        changed = self.feed("hook.codex", None, {
            "event": "UserPromptSubmit",
            "data": {"session_id": "not-yet-polled", "turn_id": "t1", "cwd": "/tmp/proj"},
        })
        self.assertEqual(changed, [])
        self.assertEqual(self.r.sessions, {})

    def test_user_prompt_submit_flips_to_working_immediately(self):
        # the latency win this issue is about: no waiting for the next poll.
        s = codex_session(state=SessionState.DONE, state_origin="poll", state_since=NOW - 100)
        self.r.sessions[s.key] = s
        self.feed("hook.codex", None, {
            "event": "UserPromptSubmit",
            "data": {"session_id": s.session_id, "turn_id": "t2", "cwd": s.cwd},
        })
        self.assertEqual(s.state, SessionState.WORKING)
        self.assertEqual(s.state_origin, "hook")

    def test_stop_flips_to_done(self):
        s = codex_session(state=SessionState.WORKING, state_origin="hook", state_since=NOW - 10)
        self.r.sessions[s.key] = s
        self.feed("hook.codex", None, {
            "event": "Stop", "data": {"session_id": s.session_id, "turn_id": "t3"},
        })
        self.assertEqual(s.state, SessionState.DONE)

    def test_session_start_touches_activity_without_forcing_a_state(self):
        s = codex_session(state=SessionState.IDLE, state_origin="poll")
        self.r.sessions[s.key] = s
        changed = self.feed("hook.codex", None, {
            "event": "SessionStart", "data": {"session_id": s.session_id},
        })
        self.assertEqual(changed, [])
        self.assertEqual(s.state, SessionState.IDLE)


if __name__ == "__main__":
    unittest.main()
