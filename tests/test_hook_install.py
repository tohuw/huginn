"""Hook installation reconciliation tests."""
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from huginn.hooks.install import CODEX_EVENTS, _hook_command, _remove_stale_huginn_events


class HookInstallTests(unittest.TestCase):
    def test_command_quotes_console_script_path(self):
        with patch("huginn.hooks.install.HOOK_BIN", Path("/tmp/Huginn Tools/huginn-hook")):
            command = _hook_command("claude", "Stop")
        self.assertIn("Huginn Tools", command)
        self.assertTrue(command.endswith("claude Stop"))

    def test_windows_command_quotes_console_executable(self):
        with (patch("huginn.hooks.install.HOOK_BIN", Path(r"C:\Program Files\Huginn\huginn-hook.exe")),
              patch.object(os, "name", "nt")):
            command = _hook_command("codex", "SessionStart")
        self.assertTrue(command.startswith('"C:\\Program Files'))
        self.assertTrue(command.endswith("codex SessionStart"))

    def test_codex_registers_only_observed_supported_events(self):
        self.assertEqual(CODEX_EVENTS, ["SessionStart", "UserPromptSubmit", "Stop"])

    def test_stale_removal_preserves_foreign_handlers(self):
        data = {"hooks": {"Notification": [{"hooks": [
            {"type": "command", "command": "huginn-hook codex Notification"},
            {"type": "command", "command": "notify-me"},
        ]}], "SessionEnd": [{"hooks": [
            {"type": "command", "command": "huginn-hook codex SessionEnd"},
        ]}], "Stop": [{"hooks": [
            {"type": "command", "command": "huginn-hook codex Stop"},
        ]}]}}
        removed = _remove_stale_huginn_events(data, CODEX_EVENTS)
        self.assertEqual(removed, 2)
        self.assertEqual(data["hooks"]["Notification"][0]["hooks"][0]["command"],
                         "notify-me")
        self.assertNotIn("SessionEnd", data["hooks"])
        self.assertIn("Stop", data["hooks"])


if __name__ == "__main__":
    unittest.main()
