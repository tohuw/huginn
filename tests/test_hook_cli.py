from __future__ import annotations

import io
import json
import os
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from huginn.hooks.cli import main


class _Stdin:
    def __init__(self, payload: bytes):
        self.buffer = io.BytesIO(payload)


class _Response:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class HookCLITests(unittest.TestCase):
    # The hook adds terminal coordinates when its terminal supplies them, so
    # these run with that environment cleared. Otherwise the assertions depend
    # on which terminal the developer happens to be using -- which is exactly
    # how this test started failing, in WezTerm, on a payload it was asserting
    # byte-for-byte.
    NO_TERMINAL = {k: "" for k in ("WEZTERM_PANE", "WEZTERM_UNIX_SOCKET",
                                   "WEZTERM_EXECUTABLE", "WEZTERM_EXECUTABLE_DIR")}

    def _forward(self, payload: bytes, env: dict[str, str] | None = None):
        with TemporaryDirectory() as tmp:
            state = Path(tmp)
            (state / "port").write_text("48200")
            (state / "token").write_text("secret")
            with (patch.dict(os.environ, env or self.NO_TERMINAL),
                  patch("huginn.hooks.cli.config.STATE_DIR", state),
                  patch("huginn.hooks.cli.config.TOKEN_PATH", state / "token"),
                  patch.object(sys, "argv", ["huginn-hook", "claude", "Stop"]),
                  patch.object(sys, "stdin", _Stdin(payload)),
                  patch("huginn.hooks.cli.urllib.request.urlopen",
                        return_value=_Response()) as open_url):
                self.assertEqual(main(), 0)
        return open_url.call_args.args[0]

    def test_forwards_payload_token_and_dynamic_port(self):
        request = self._forward(b'{"session_id":"abc"}')
        self.assertEqual(request.full_url, "http://127.0.0.1:48200/api/hook/claude/Stop")
        self.assertEqual(request.data, b'{"session_id":"abc"}')
        self.assertEqual(request.headers["X-huginn-token"], "secret")

    def test_a_terminal_that_can_locate_itself_is_forwarded_too(self):
        """The pane id has to reach the daemon, and this is its only route."""
        with TemporaryDirectory() as tmp:
            cli = Path(tmp) / ("wezterm.exe" if os.name == "nt" else "wezterm")
            cli.write_bytes(b"")
            request = self._forward(b'{"session_id":"abc"}', {
                "WEZTERM_PANE": "4",
                "WEZTERM_UNIX_SOCKET": "/tmp/sock",
                "WEZTERM_EXECUTABLE": str(cli),
                "WEZTERM_EXECUTABLE_DIR": tmp,
            })
        body = json.loads(request.data)
        self.assertEqual(body["session_id"], "abc")
        self.assertEqual(body["huginn_terminal"]["pane"], "4")

    def test_the_original_payload_is_never_lost(self):
        """A hook that mangles what the agent sent is worse than no hook."""
        request = self._forward(b'{"session_id":"abc","prompt":"hi"}')
        self.assertEqual(json.loads(request.data),
                         {"session_id": "abc", "prompt": "hi"})

    def test_missing_args_and_bad_json_never_fail(self):
        with patch.object(sys, "argv", ["huginn-hook"]):
            self.assertEqual(main(), 0)
        with (patch.object(sys, "argv", ["huginn-hook", "codex", "Stop"]),
              patch.object(sys, "stdin", _Stdin(b"not json"))):
            self.assertEqual(main(), 0)

    def test_absent_stdin_never_fails(self):
        """The windowless forwarder may be handed no stdin at all.

        The hook is launched from a GUI-subsystem executable so no console
        window appears, and such a process can have ``sys.stdin`` as None
        rather than an empty stream. Reading ``.buffer`` off None raises
        AttributeError, which is the one thing a hook must never do to the
        agent process it is attached to. Platform-independent: None is None.
        """
        with (patch.object(sys, "argv", ["huginn-hook", "claude", "Stop"]),
              patch.object(sys, "stdin", None)):
            self.assertEqual(main(), 0)
