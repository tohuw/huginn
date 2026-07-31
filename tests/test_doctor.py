"""Doctor daemon health check authentication."""
from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from huginn import config
from huginn.doctor import (
    TESTED_CLAUDE,
    TESTED_CODEX,
    _check_version_coverage,
    _daemon_session_count,
    _report_plugins,
)
from huginn.plugins import API_VERSION, MIN_API_VERSION, PluginSpec, discover_plugins


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class _Sess:
    def __init__(self, version):
        self.version = version


class VersionCoverageTests(unittest.TestCase):
    """issue #22: TESTED_CODEX was declared but never actually checked."""

    def test_warns_when_newer_than_tested(self):
        newer = f"{TESTED_CLAUDE[0]}.{TESTED_CLAUDE[1] + 1}.0"
        with _capture_stdout() as out:
            _check_version_coverage("claude", [_Sess(newer)], TESTED_CLAUDE)
        self.assertIn("newer than tested", out.getvalue())

    def test_no_warning_at_or_below_tested(self):
        same = f"{TESTED_CODEX[0]}.{TESTED_CODEX[1]}.9"
        with _capture_stdout() as out:
            _check_version_coverage("codex", [_Sess(same)], TESTED_CODEX)
        self.assertEqual(out.getvalue(), "")

    def test_only_checks_the_first_versioned_session(self):
        newer = f"{TESTED_CODEX[0]}.{TESTED_CODEX[1] + 1}.0"
        with _capture_stdout() as out:
            _check_version_coverage("codex", [_Sess(None), _Sess(newer), _Sess("99.99.0")],
                                    TESTED_CODEX)
        # exactly one warning, for the first *versioned* session
        self.assertEqual(out.getvalue().count("newer than tested"), 1)
        self.assertIn(newer, out.getvalue())


def _capture_stdout():
    import contextlib
    return contextlib.redirect_stdout(io.StringIO())


class _EntryPoint:
    def __init__(self, name, value):
        self.name = name
        self.value = value

    def load(self):
        return self.value


class PluginApiReportTests(unittest.TestCase):
    """issue #38: doctor was the *only* place a version mismatch showed up, and
    it showed up indistinguishably from an import failure. It must now name the
    mismatch and core's supported range, and still fail the run."""

    def test_api_mismatch_is_labelled_and_fails_the_check(self):
        registry = discover_plugins([_EntryPoint("stale", PluginSpec(
            name="stale", version="1", api_version=API_VERSION + 1))])

        with _capture_stdout() as out:
            ok = _report_plugins(registry)

        self.assertFalse(ok)
        text = out.getvalue()
        self.assertIn("stale API mismatch", text)
        self.assertIn(f"Huginn supports API {MIN_API_VERSION}..{API_VERSION}", text)

    def test_a_plugin_declaring_a_range_reports_that_range(self):
        registry = discover_plugins([_EntryPoint("ranged", PluginSpec(
            name="ranged", version="2.0", min_api=MIN_API_VERSION, max_api=API_VERSION + 4))])

        with _capture_stdout() as out:
            ok = _report_plugins(registry)

        self.assertTrue(ok)
        self.assertIn(f"API {MIN_API_VERSION}..{API_VERSION + 4}", out.getvalue())

    def test_no_plugins_installed_still_passes(self):
        with _capture_stdout() as out:
            ok = _report_plugins(discover_plugins([]))

        self.assertTrue(ok)
        self.assertIn("installed plugins", out.getvalue())


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
