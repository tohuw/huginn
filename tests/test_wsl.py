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

    @patch("huginn.sources.wsl.subprocess.run")
    def test_a_listed_distribution_counts_as_available(self, run):
        # --list --quiet writes UTF-16LE, so this is bytes on purpose.
        run.return_value.returncode = 0
        run.return_value.stdout = "Ubuntu-24.04\r\n".encode("utf-16-le")
        self.assertTrue(wsl.available())

    @patch("huginn.sources.wsl.subprocess.run")
    def test_wsl_exe_existing_is_not_evidence_wsl_does(self, run):
        """The false alarm. wsl.exe ships with Windows either way.

        A machine with no distribution has the binary and gets a non-zero exit
        with "The Windows Subsystem for Linux is not installed." The poller
        could not tell that from a real probe failure, so it reported itself
        failing every five seconds forever -- and spawned a process each time.
        """
        run.return_value.returncode = 1
        run.return_value.stdout = (
            "The Windows Subsystem for Linux is not installed.".encode("utf-16-le"))
        self.assertFalse(wsl.available())

    @patch("huginn.sources.wsl.subprocess.run")
    def test_installed_with_no_distributions_is_not_available(self, run):
        run.return_value.returncode = 0
        run.return_value.stdout = b"\x00\x00"
        self.assertFalse(wsl.available())

    @patch("huginn.sources.wsl.subprocess.run", side_effect=FileNotFoundError)
    def test_a_missing_binary_is_not_available(self, run):
        self.assertFalse(wsl.available())

    def test_helper_uses_current_codex_recency_columns(self):
        self.assertIn("updated_at_ms", wsl._HELPER)
        self.assertIn("recency_at_ms", wsl._HELPER)


if __name__ == "__main__":
    unittest.main()
