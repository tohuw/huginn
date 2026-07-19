"""Platform guards for daemon login installation."""
from __future__ import annotations

import io
import unittest
from contextlib import redirect_stderr
from unittest.mock import patch

from huginn import agent_install


class AgentInstallPlatformTests(unittest.TestCase):
    def test_install_does_not_call_launchctl_off_macos(self):
        with (patch.object(agent_install.sys, "platform", "win32"),
              patch.object(agent_install, "_launchctl") as launchctl,
              redirect_stderr(io.StringIO()) as stderr):
            self.assertEqual(agent_install.install(), 2)
        launchctl.assert_not_called()
        self.assertIn("only available on macOS", stderr.getvalue())

    def test_uninstall_does_not_touch_files_off_macos(self):
        with (patch.object(agent_install.sys, "platform", "win32"),
              patch.object(agent_install, "PLIST_PATH") as plist_path,
              redirect_stderr(io.StringIO()) as stderr):
            self.assertEqual(agent_install.uninstall(), 2)
        plist_path.exists.assert_not_called()
        self.assertIn("only available on macOS", stderr.getvalue())

    def test_launchctl_itself_is_guarded(self):
        with patch.object(agent_install.sys, "platform", "win32"):
            with self.assertRaisesRegex(RuntimeError, "only available on macOS"):
                agent_install._launchctl("list")


if __name__ == "__main__":
    unittest.main()
