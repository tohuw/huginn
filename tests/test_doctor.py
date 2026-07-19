"""Doctor daemon health check authentication."""
from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from huginn import config
from huginn.doctor import _daemon_session_count


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class DoctorTests(unittest.TestCase):
    def test_daemon_check_sends_current_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_token_path = config.TOKEN_PATH
            config.TOKEN_PATH = Path(tmp) / "token"
            config.TOKEN_PATH.write_text("current-token\n")
            try:
                def fake_urlopen(request, timeout):
                    self.assertEqual(request.full_url,
                                     "http://127.0.0.1:47100/api/sessions")
                    self.assertEqual(request.get_header("X-huginn-token"),
                                     "current-token")
                    self.assertEqual(timeout, 2)
                    return _Response(json.dumps({"sessions": [{}, {}]}).encode())

                with patch("urllib.request.urlopen", side_effect=fake_urlopen):
                    self.assertEqual(_daemon_session_count(47100), 2)
            finally:
                config.TOKEN_PATH = old_token_path


if __name__ == "__main__":
    unittest.main()
