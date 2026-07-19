from __future__ import annotations

import io
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
    def test_forwards_payload_token_and_dynamic_port(self):
        with TemporaryDirectory() as tmp:
            state = Path(tmp)
            (state / "port").write_text("48200")
            (state / "token").write_text("secret")
            with (patch("huginn.hooks.cli.config.STATE_DIR", state),
                  patch("huginn.hooks.cli.config.TOKEN_PATH", state / "token"),
                  patch.object(sys, "argv", ["huginn-hook", "claude", "Stop"]),
                  patch.object(sys, "stdin", _Stdin(b'{"session_id":"abc"}')),
                  patch("huginn.hooks.cli.urllib.request.urlopen", return_value=_Response()) as open_url):
                self.assertEqual(main(), 0)
        request = open_url.call_args.args[0]
        self.assertEqual(request.full_url, "http://127.0.0.1:48200/api/hook/claude/Stop")
        self.assertEqual(request.data, b'{"session_id":"abc"}')
        self.assertEqual(request.headers["X-huginn-token"], "secret")

    def test_missing_args_and_bad_json_never_fail(self):
        with patch.object(sys, "argv", ["huginn-hook"]):
            self.assertEqual(main(), 0)
        with (patch.object(sys, "argv", ["huginn-hook", "codex", "Stop"]),
              patch.object(sys, "stdin", _Stdin(b"not json"))):
            self.assertEqual(main(), 0)
