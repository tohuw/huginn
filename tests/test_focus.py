"""Focus routing keeps CLI agents in their terminal application."""
from __future__ import annotations

import unittest
from unittest.mock import patch

from huginn.focus import focus_session
from huginn.model import Session


def session(*, source: str = "codex", entrypoint: str = "cli") -> Session:
    return Session(
        key="codex:test", source=source, session_id="test",
        cwd="/tmp/project", name="project-test", entrypoint=entrypoint,
    )


class FocusRoutingTests(unittest.TestCase):
    def test_codex_cli_focuses_matching_iterm_cwd(self):
        with patch("huginn.focus._codex_tty_for_cwd", return_value="ttys005") as find, \
             patch("huginn.focus._focus_iterm_tty", return_value=True) as focus, \
             patch("huginn.focus._open_app") as open_app:
            result = focus_session(session())
        find.assert_called_once_with("/tmp/project")
        focus.assert_called_once_with("ttys005")
        open_app.assert_not_called()
        self.assertEqual(result["target"], "iTerm2")

    def test_non_cli_codex_opens_chatgpt(self):
        with patch("huginn.focus._open_app", return_value=True) as open_app, \
             patch("huginn.focus._codex_tty_for_cwd") as focus:
            result = focus_session(session(entrypoint="app-server"))
        open_app.assert_called_once_with("ChatGPT")
        focus.assert_not_called()
        self.assertEqual(result["target"], "ChatGPT")

    def test_missing_codex_terminal_never_falls_through_to_vscode(self):
        with patch("huginn.focus._codex_tty_for_cwd", return_value=None), \
             patch("huginn.focus._platform.focus_terminal") as terminal, \
             patch("huginn.focus._platform.focus_vscode") as vscode:
            terminal.return_value.ok = False
            terminal.return_value.detail = "iTerm2 tab not found"
            result = focus_session(session())
        vscode.assert_not_called()
        self.assertFalse(result["ok"])

    def test_wsl_codex_never_opens_chatgpt(self):
        wsl = session(entrypoint="wsl:cli")
        wsl.key = "wsl:Ubuntu:codex:test"
        with patch("huginn.focus._open_app") as open_app, \
             patch("huginn.focus._platform.focus_vscode") as vscode, \
             patch("huginn.focus._platform.focus_terminal") as terminal:
            vscode.return_value.ok = False
            terminal.return_value.ok = True
            terminal.return_value.target = "Windows Terminal"
            terminal.return_value.detail = "exact tab unavailable"
            result = focus_session(wsl)
        open_app.assert_not_called()
        terminal.assert_called_once_with(None, None)
        self.assertEqual(result["target"], "Windows Terminal")


if __name__ == "__main__":
    unittest.main()
