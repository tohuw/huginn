"""Dialect analyzers: Claude transcript entries and Codex rollout entries.

Moved from huginn's tests/test_reducer.py -- issue #42: the analyzers now live in
corvidae, so their unit coverage lives with them. huginn keeps the tests that
exercise the *reducer* wiring downstream of an analyzer.
"""
from __future__ import annotations

import unittest

from corvidae.transcript import ClaudeAnalyzer, CodexAnalyzer


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


if __name__ == "__main__":
    unittest.main()
