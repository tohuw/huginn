"""Authority, exact-input validation, and one-use steering confirmation."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from huginn.model import Session
from huginn.platform.base import FocusResult
from huginn.platform.macos import MacOSPlatform
from huginn.platform.windows import WindowsPlatform
from huginn.steering import (
    ConfirmationStore,
    authority_for,
    execute_pending,
    send_instruction,
    set_authority,
    validate_instruction,
)


def _session(session_id: str = "thread-1") -> Session:
    return Session(
        key="codex:thread-1",
        source="codex",
        session_id=session_id,
        cwd="/tmp/project",
        name="project-thread",
        entrypoint="cli",
    )


class InstructionValidationTests(unittest.TestCase):
    def test_valid_instruction_is_preserved_exactly(self):
        self.assertEqual(validate_instruction("  continue here  "), "  continue here  ")

    def test_multiline_instruction_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "one line"):
            validate_instruction("first\nsecond")

    def test_control_character_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "control"):
            validate_instruction("first\tsecond")

    def test_oversized_instruction_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "800"):
            validate_instruction("x" * 801)


class AuthorityTests(unittest.TestCase):
    def test_authority_file_is_private_and_bound_to_session_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state" / "authorities.json"
            with patch("huginn.steering._terminal_target", return_value=(None, "ttys001")):
                result = set_authority(_session(), "steer", path)

            self.assertEqual(result["level"], "steer")
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(authority_for(_session(), path), "steer")
            self.assertEqual(authority_for(_session("reused-process"), path), "observe")

    def test_observe_revokes_persisted_steering(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "authorities.json"
            with patch("huginn.steering._terminal_target", return_value=(None, "ttys001")):
                set_authority(_session(), "steer", path)

            set_authority(_session(), "observe", path)

            self.assertEqual(authority_for(_session(), path), "observe")

    def test_unknown_authority_level_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "observe or steer"):
            set_authority(_session(), "operate", Path("/unused"))


class SteeringExecutionTests(unittest.TestCase):
    def test_codex_target_fails_closed_when_workspace_has_multiple_tabs(self):
        process_ttys = {101: "ttys001", 202: "ttys002"}
        with patch("huginn.steering._platform.find_processes", return_value=[101, 202]), \
             patch("huginn.steering._platform.process_cwd", return_value="/tmp/project"), \
             patch("huginn.steering._platform.process_tty", side_effect=process_ttys.get):
            with self.assertRaisesRegex(ValueError, "multiple Codex terminal tabs"):
                set_authority(_session(), "steer", Path("/unused"))

    def test_send_passes_exact_text_as_platform_argument(self):
        platform_result = FocusResult(True, "iTerm2")
        with patch("huginn.steering.authority_for", return_value="steer"), \
             patch("huginn.steering._terminal_target", return_value=(123, "ttys001")), \
             patch("huginn.steering._platform.send_terminal_text", return_value=platform_result) as send:
            result = send_instruction(_session(), "  exact input  ")

        send.assert_called_once_with(123, "ttys001", "  exact input  ")
        self.assertTrue(result["ok"])

    def test_send_fails_closed_without_steer_authority(self):
        with patch("huginn.steering.authority_for", return_value="observe"):
            with self.assertRaises(PermissionError):
                send_instruction(_session(), "continue")

    def test_macos_adapter_passes_instruction_as_osascript_argv(self):
        adapter = MacOSPlatform()
        with patch("huginn.platform.macos.run", return_value="ok") as run:
            result = adapter.send_terminal_text(None, "ttys001", "value; $(not-shell)")

        command = run.call_args.args[0]
        self.assertEqual(command[-2:], ["/dev/ttys001", "value; $(not-shell)"])
        self.assertTrue(result.ok)

    def test_windows_steering_fails_closed_without_exact_tab(self):
        result = WindowsPlatform().send_terminal_text(10, None, "continue")

        self.assertFalse(result.ok)
        self.assertIn("not available", result.detail)


class ConfirmationTests(unittest.TestCase):
    def test_confirmation_preview_contains_exact_json_quoted_line(self):
        store = ConfirmationStore(now=lambda: 100)
        with patch("huginn.steering.authority_for", return_value="steer"), \
             patch("huginn.steering._terminal_target", return_value=(None, "ttys001")):
            pending = store.create(_session(), "send", "  exact input  ")

        self.assertIn('"  exact input  "', pending.summary)

    def test_confirmation_is_one_use(self):
        store = ConfirmationStore(now=lambda: 100)
        with patch("huginn.steering.authority_for", return_value="steer"), \
             patch("huginn.steering._terminal_target", return_value=(None, "ttys001")):
            pending = store.create(_session(), "interrupt")

        self.assertEqual(store.consume(pending.confirmation_id), pending)
        with self.assertRaisesRegex(ValueError, "already used"):
            store.consume(pending.confirmation_id)

    def test_expired_confirmation_is_rejected(self):
        clock = [100.0]
        store = ConfirmationStore(now=lambda: clock[0])
        with patch("huginn.steering.authority_for", return_value="steer"), \
             patch("huginn.steering._terminal_target", return_value=(None, "ttys001")):
            pending = store.create(_session(), "interrupt")
        clock[0] = 161.0

        with self.assertRaisesRegex(ValueError, "expired"):
            store.consume(pending.confirmation_id)

    def test_execution_rejects_reused_session_key(self):
        store = ConfirmationStore(now=lambda: 100)
        with patch("huginn.steering.authority_for", return_value="steer"), \
             patch("huginn.steering._terminal_target", return_value=(None, "ttys001")):
            pending = store.create(_session(), "interrupt")

        with self.assertRaisesRegex(ValueError, "session changed"):
            execute_pending(pending, _session("different-session"))


if __name__ == "__main__":
    unittest.main()
