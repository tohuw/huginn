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

    def test_windows_prefers_the_windowless_forwarder(self):
        """Hooks fire constantly; a console build makes a window each time.

        huginn-hook.exe is console-subsystem, so every launch without a console
        to inherit made Windows allocate one -- measured at two windows per
        launch on Windows 11, where that surfaces as Windows Terminal opening
        and closing. huginn-hookw.exe is the same forwarder, GUI subsystem.
        """
        from huginn.hooks import install

        with patch.object(install.os, "name", "nt"), \
             patch.object(install.shutil, "which",
                          side_effect=lambda name: {
                              "huginn-hookw": r"C:\venv\Scripts\huginn-hookw.exe",
                              "huginn-hook": r"C:\venv\Scripts\huginn-hook.exe",
                          }.get(name)):
            assert install._hook_bin().name == "huginn-hookw.exe"

    @unittest.skipUnless(os.name == "nt", "constructs a real Path under os.name='nt'")
    def test_an_older_install_without_the_windowless_build_still_works(self):
        """Windows-only because of how it has to be faked, not what it asserts.

        ``patch.object(install.os, "name", "nt")`` mutates the *global* os
        module -- there is only one -- so pathlib then hands back WindowsPath,
        which cannot be instantiated off Windows. The sibling test above
        survives that because ``which`` answers and it returns before reaching
        ``Path(sys.executable)``; this one takes the fallback branch that does.
        Covered by the Windows CI job.
        """
        from huginn.hooks import install

        with patch.object(install.os, "name", "nt"), \
             patch.object(install.shutil, "which",
                          side_effect=lambda name: None if name == "huginn-hookw"
                          else r"C:\venv\Scripts\huginn-hook.exe"), \
             patch.object(Path, "exists", lambda _self: False):
            assert install._hook_bin().name == "huginn-hook.exe"

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
