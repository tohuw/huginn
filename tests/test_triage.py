"""Deterministic triage and real-worktree contention behavior."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from huginn.model import Session, SessionState
from huginn.triage import build_triage, worktree_root


def _session(name: str, cwd: str, state: SessionState, source: str = "codex") -> Session:
    return Session(
        key=f"{source}:{name}",
        source=source,
        session_id=name,
        cwd=cwd,
        name=name,
        state=state,
        state_since=900,
    )


class TriageTests(unittest.TestCase):
    def test_waiting_session_is_in_attention_bucket(self):
        session = _session("review", "/tmp/review", SessionState.WAITING_PERMISSION)

        result = build_triage([session], now=1000)

        self.assertEqual(result["verdict"]["level"], "attention")
        self.assertEqual(result["attention"][0]["reason"], "waiting for permission")

    def test_working_sessions_in_nested_directories_contend_on_same_worktree(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            (root / ".git").mkdir(parents=True)
            first_dir = root / "src"
            second_dir = root / "tests"
            first_dir.mkdir()
            second_dir.mkdir()
            first = _session("one", str(first_dir), SessionState.WORKING)
            second = _session("two", str(second_dir), SessionState.WORKING, "claude")

            result = build_triage([first, second], now=1000)

        self.assertEqual(result["verdict"]["level"], "contention")
        self.assertEqual(result["contentions"][0]["worktree"], str(root.resolve()))
        self.assertEqual(result["contentions"][0]["count"], 2)

    def test_separate_worktrees_with_same_basename_do_not_contend(self):
        with tempfile.TemporaryDirectory() as tmp:
            first_root = Path(tmp) / "primary" / "project"
            second_root = Path(tmp) / "feature" / "project"
            (first_root / ".git").mkdir(parents=True)
            (second_root / ".git").mkdir(parents=True)
            first = _session("one", str(first_root), SessionState.WORKING)
            second = _session("two", str(second_root), SessionState.WORKING, "claude")

            result = build_triage([first, second], now=1000)

        self.assertEqual(result["contentions"], [])

    def test_done_session_does_not_create_contention(self):
        first = _session("one", "/tmp/project", SessionState.WORKING)
        second = _session("two", "/tmp/project", SessionState.DONE, "claude")

        result = build_triage([first, second], now=1000)

        self.assertEqual(result["contentions"], [])

    def test_desktop_activity_does_not_create_worktree_contention(self):
        first = _session("one", "/tmp/project", SessionState.WORKING)
        desktop = _session(
            "desktop", "/tmp/project", SessionState.WORKING, "chatgpt-desktop")

        result = build_triage([first, desktop], now=1000)

        self.assertEqual(result["contentions"], [])

    def test_non_git_sessions_contend_only_in_same_canonical_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            root.mkdir()
            first = _session("one", str(root), SessionState.WORKING)
            second = _session(
                "two", str(root / ".." / "project"), SessionState.WORKING, "claude")

            result = build_triage([first, second], now=1000)

        self.assertEqual(result["contentions"][0]["worktree"], str(root.resolve()))

    def test_relative_path_is_not_resolved_against_daemon_cwd(self):
        self.assertIsNone(worktree_root("relative/project"))


if __name__ == "__main__":
    unittest.main()
