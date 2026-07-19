import json
import subprocess
import unittest
from unittest.mock import patch

from huginn.model import SessionState
from huginn.sources import wsl


class WslSourceTests(unittest.TestCase):
    def test_translates_mnt_paths(self):
        self.assertEqual(wsl.windows_path("/mnt/c/Users/me/code"), r"C:\Users\me\code")
        self.assertEqual(wsl.windows_path("/home/me/code"), "/home/me/code")

    @patch("huginn.sources.wsl.subprocess.run")
    def test_normalizes_and_namespaces_sessions(self, run):
        run.return_value.returncode = 0
        run.return_value.stdout = json.dumps([{
            "source": "claude", "id": "abc", "pid": 44,
            "cwd": "/mnt/d/work/project", "state": "working", "updated": 100,
        }])
        sessions, ok = wsl.scan("Ubuntu 24.04")
        self.assertTrue(ok)
        self.assertEqual(sessions[0].key, "wsl:Ubuntu-24.04:claude:abc")
        self.assertEqual(sessions[0].cwd, r"D:\work\project")
        self.assertEqual(sessions[0].state, SessionState.WORKING)
        self.assertIsNone(sessions[0].pid)
        command = run.call_args.args[0]
        self.assertEqual(command[:3], ["wsl.exe", "--distribution", "Ubuntu 24.04"])

    @patch("huginn.sources.wsl.subprocess.run", side_effect=FileNotFoundError)
    def test_absent_wsl_degrades_cleanly(self, run):
        self.assertEqual(wsl.scan(), ([], False))

    @patch("huginn.sources.wsl.subprocess.run", side_effect=subprocess.TimeoutExpired("wsl", 8))
    def test_timeout_degrades_cleanly(self, run):
        self.assertEqual(wsl.scan(), ([], False))

    @patch("huginn.sources.wsl.subprocess.run")
    def test_malformed_output_is_failure(self, run):
        run.return_value.returncode = 0
        run.return_value.stdout = "not json"
        self.assertEqual(wsl.scan(), ([], False))

    def test_helper_uses_current_codex_recency_columns(self):
        self.assertIn("updated_at_ms", wsl._HELPER)
        self.assertIn("recency_at_ms", wsl._HELPER)


if __name__ == "__main__":
    unittest.main()
