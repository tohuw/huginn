"""Best-effort ChatGPT Desktop discovery without reading conversation data."""
from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from huginn.model import SessionState
from huginn.sources import chatgpt_desktop


class ChatGPTDesktopTests(unittest.TestCase):
    def test_absent_process_has_no_tile(self):
        with patch("huginn.sources.chatgpt_desktop._app_pids", return_value=[]):
            self.assertIsNone(chatgpt_desktop.scan())

    def test_recent_local_activity_marks_app_active(self):
        with tempfile.TemporaryDirectory() as tmp:
            support = Path(tmp)
            log = support / "desktop.log"
            log.write_text("activity")
            with patch("huginn.sources.chatgpt_desktop._app_pids", return_value=[42]), \
                 patch("huginn.sources.chatgpt_desktop._support_dirs", return_value=[support]):
                session = chatgpt_desktop.scan()
        self.assertIsNotNone(session)
        self.assertEqual(session.state, SessionState.ACTIVE)
        self.assertEqual(session.pid, 42)
        self.assertEqual(session.source, "chatgpt-desktop")

    def test_old_activity_keeps_presence_but_marks_idle(self):
        with patch("huginn.sources.chatgpt_desktop._app_pids", return_value=[42]), \
             patch("huginn.sources.chatgpt_desktop._latest_activity",
                   return_value=time.time() - chatgpt_desktop.ACTIVE_S - 1):
            session = chatgpt_desktop.scan()
        self.assertEqual(session.state, SessionState.IDLE)


class AppVersusCliTests(unittest.TestCase):
    """The name is not identity, and that is what kept a dead tile alive.

    The Store package OpenAI.Codex ships both ChatGPT.exe and Codex.exe, the npm
    CLI is also codex.exe, and Windows matches names case-insensitively. So the
    tile said ChatGPT Desktop was running for an hour and a half after it closed
    -- it was really watching the CLI.
    """

    def _pids(self, paths: dict[int, str], platform: str = "win32"):
        return patch.multiple(
            "huginn.sources.chatgpt_desktop",
            sys=type("s", (), {"platform": platform})(),
            _platform=type("p", (), {
                "find_processes": staticmethod(lambda name: list(paths)),
                "process_path": staticmethod(lambda pid: paths.get(pid)),
            })(),
        )

    def test_the_npm_cli_is_not_the_desktop_app(self):
        cli = r"C:\Users\me\AppData\Roaming\npm\node_modules\@openai\codex\bin\codex.exe"
        with self._pids({1: cli}):
            self.assertEqual(chatgpt_desktop._app_pids(), [])

    def test_a_versioned_cli_install_is_not_the_desktop_app(self):
        """The second CLI layout on the same machine, which a denylist missed."""
        cli = r"C:\Users\me\AppData\Local\OpenAI\Codex\bin\8e8bf206\codex.exe"
        with self._pids({1: cli}):
            self.assertEqual(chatgpt_desktop._app_pids(), [])

    def test_the_store_package_is_the_desktop_app(self):
        app = (r"C:\Program Files\WindowsApps"
               r"\OpenAI.Codex_26.803.10989.0_x64__2p2nqsd0c76g0\app\ChatGPT.exe")
        with self._pids({7: app}):
            self.assertEqual(chatgpt_desktop._app_pids(), [7])

    def test_a_codex_exe_inside_the_package_still_counts(self):
        app = (r"C:\Program Files\WindowsApps"
               r"\OpenAI.Codex_26.803.10989.0_x64__2p2nqsd0c76g0\app\Codex.exe")
        with self._pids({7: app}):
            self.assertEqual(chatgpt_desktop._app_pids(), [7])

    def test_a_macos_bundle_counts(self):
        with self._pids({7: "/Applications/ChatGPT.app/Contents/MacOS/ChatGPT"}, "darwin"):
            self.assertEqual(chatgpt_desktop._app_pids(), [7])

    def test_a_macos_cli_does_not(self):
        with self._pids({7: "/opt/homebrew/bin/codex"}, "darwin"):
            self.assertEqual(chatgpt_desktop._app_pids(), [])

    def test_an_unreadable_path_is_not_claimed_as_running(self):
        """Deliberately the strict direction: this source's job is presence."""
        with self._pids({1: None}):
            self.assertEqual(chatgpt_desktop._app_pids(), [])


if __name__ == "__main__":
    unittest.main()
