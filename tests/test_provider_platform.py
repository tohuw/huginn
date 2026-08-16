import os
import subprocess
import unittest
from unittest.mock import patch

from huginn.llm.providers import _spawn_options, claude_binary, codex_binary
import huginn.llm.providers as providers


class ProviderPlatformTests(unittest.TestCase):
    def tearDown(self):
        providers._claude_path = None

    def test_posix_processes_start_in_new_session(self):
        with patch.object(os, "name", "posix"):
            self.assertEqual(_spawn_options(), {"start_new_session": True})

    def test_windows_processes_start_in_new_process_group(self):
        with patch.object(os, "name", "nt"):
            flags = _spawn_options()["creationflags"]
        self.assertTrue(flags & getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200))

    def test_windows_processes_open_no_console_window(self):
        """The daemon runs under pythonw and has no console to lend a child.

        The provider CLIs are console-subsystem programs, so without this flag
        Windows gives each invocation a console of its own — measured at one
        terminal window per call, and automatic enrichment made dozens appear.
        CREATE_NEW_PROCESS_GROUP does not imply it; it only controls signals.
        """
        with patch.object(os, "name", "nt"):
            flags = _spawn_options()["creationflags"]
        self.assertTrue(flags & getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000))

    def test_windows_claude_lookup_does_not_invoke_zsh(self):
        providers._claude_path = None
        with (patch.object(os, "name", "nt"),
              patch("huginn.llm.providers.shutil.which", side_effect=[r"C:\\bin\\claude.exe"]),
              patch("huginn.llm.providers.subprocess.run") as run):
            self.assertEqual(claude_binary(), r"C:\\bin\\claude.exe")
            run.assert_not_called()

    def test_windows_codex_lookup_uses_path(self):
        with (patch.object(os, "name", "nt"),
              patch("huginn.llm.providers.shutil.which", return_value=r"C:\\bin\\codex.exe")):
            self.assertEqual(codex_binary(), r"C:\\bin\\codex.exe")
