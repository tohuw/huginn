"""Doctor daemon health check authentication and data-lag reporting."""
from __future__ import annotations

import io
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from huginn import config, doctor, lag
from huginn.doctor import (
    TESTED_CLAUDE,
    TESTED_CODEX,
    _check_version_coverage,
    _daemon_session_count,
    _report_data_lag,
    _report_model_policy,
    _report_plugins,
    _snapshot_sessions,
)
from huginn.plugins import (
    API_VERSION,
    MIN_API_VERSION,
    PluginLoadError,
    PluginRegistry,
    PluginSpec,
    discover_plugins,
)
from huginn.policy import ModelPolicy


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


class ModelPolicyReportTests(unittest.TestCase):
    """issue #41: a refused configured model must be visible in doctor, not
    only at the first Ask or automatic-text call."""

    @staticmethod
    def _installed(*policies):
        points = [_EntryPoint(policy.name, policy) for policy in policies]
        return patch("huginn.policy.entry_points", return_value=points)

    def test_no_installed_policy_reports_unrestricted_and_passes(self):
        with patch("huginn.policy.entry_points", return_value=[]), _capture_stdout() as out:
            ok = _report_model_policy(config.Config({}))

        self.assertTrue(ok)
        self.assertIn("every model permitted", out.getvalue())

    def test_refused_configured_model_fails_with_the_reason_verbatim(self):
        policy = ModelPolicy(name="bedrock-only", allow=(r"^us\.anthropic\.",),
                             require_provider="bedrock",
                             reason="POLICY_REASON_TOKEN: approved provider only")
        cfg = config.Config({"llm": {"provider": "claude", "chat_model": "sonnet",
                                     "blurb_model": "haiku"}})

        with self._installed(policy), _capture_stdout() as out:
            ok = _report_model_policy(cfg)

        self.assertFalse(ok)
        self.assertIn("POLICY_REASON_TOKEN", out.getvalue())

    def test_permitted_configured_model_passes_and_lists_the_policy(self):
        policy = ModelPolicy(name="bedrock-only", allow=(r"^us\.anthropic\.",),
                             require_provider="bedrock", reason="approved provider only")
        cfg = config.Config({"llm": {
            "provider": "bedrock",
            "chat_model": "us.anthropic.claude-sonnet-5",
            "blurb_model": "us.anthropic.claude-haiku-4-5",
        }})

        with self._installed(policy), _capture_stdout() as out:
            ok = _report_model_policy(cfg)

        self.assertTrue(ok)
        self.assertIn("bedrock-only", out.getvalue())

    def test_shadowed_policy_distributions_are_reported_as_a_failure(self):
        # issue #41 C1: resolution now sees through a metadata-only shadow, but
        # the name collision itself is the signal an exclusion is under attack.
        with patch("huginn.policy.shadowed_policy_distributions", return_value=("mypol",)), \
             patch("huginn.policy.entry_points", return_value=[]), _capture_stdout() as out:
            ok = _report_model_policy(config.Config({}))

        self.assertFalse(ok)
        self.assertIn("shadowed", out.getvalue())
        self.assertIn("mypol", out.getvalue())


class DoctorOutputSanitizationTests(unittest.TestCase):
    """issue #41 M4: doctor emits its own ANSI codes and printed policy/plugin
    labels verbatim, so an embedded ``\\x1b[2K\\r`` could erase and rewrite the
    line already printed -- enough to forge a green "installed policies — none
    (every model permitted)". Sources are trusted-ish, so this is low severity,
    but doctor's output is the evidence issue #41 relies on."""

    FORGERY = "\x1b[2K\rharmless \033[32m✓\033[0m installed policies — none"

    def test_control_characters_are_stripped_from_labels_and_details(self):
        self.assertNotIn("\x1b", doctor.safe(self.FORGERY))
        self.assertNotIn("\r", doctor.safe(self.FORGERY))
        self.assertNotIn("\n", doctor.safe("line one\nline two"))
        self.assertNotIn("\x7f", doctor.safe("a\x7fb"))

    def test_long_labels_are_truncated(self):
        self.assertLessEqual(len(doctor.safe("x" * 5000)), doctor.MAX_LABEL_CHARS + 1)

    def test_ordinary_text_including_unicode_is_left_intact(self):
        self.assertEqual(doctor.safe("us.anthropic.claude-sonnet-5 — permitted"),
                         "us.anthropic.claude-sonnet-5 — permitted")

    def test_a_policy_cannot_forge_a_line_in_the_report(self):
        policy = ModelPolicy(name=self.FORGERY, allow=("^ok$",), reason=self.FORGERY)
        points = [_EntryPoint("forger", policy)]

        with patch("huginn.policy.entry_points", return_value=points), _capture_stdout() as out:
            _report_model_policy(config.Config({"llm": {"provider": "claude"}}))

        # No escape sequence beyond doctor's own colour codes for its marks.
        self.assertNotIn("\x1b[2K", out.getvalue())
        self.assertNotIn("\r", out.getvalue())

    def test_a_plugin_error_cannot_forge_a_line_in_the_report(self):
        registry = PluginRegistry(errors=(PluginLoadError(
            entry_point=self.FORGERY, error_class=self.FORGERY, detail=self.FORGERY),))

        with _capture_stdout() as out:
            _report_plugins(registry)

        self.assertNotIn("\x1b[2K", out.getvalue())
        self.assertNotIn("\r", out.getvalue())

    def test_a_plugin_supplied_source_name_cannot_forge_a_lag_line(self):
        # entry.source reaches the report from a plugin or a restored snapshot.
        with _capture_stdout() as out:
            _report_data_lag(
                [{"key": "plugin:a:1", "source": self.FORGERY, "last_activity": 100.0}],
                config.Config({}))

        self.assertNotIn("\x1b[2K", out.getvalue())
        self.assertNotIn("\r", out.getvalue())
class _PluginSource:
    """Plugin source that opts into artifact-time reporting -- issue #39."""

    name = "workers"

    def __init__(self, mtime=None, error=None):
        self._mtime = mtime
        self._error = error

    async def run(self, context):   # pragma: no cover - contract only
        context.ok()

    def artifact_mtime(self):
        if self._error:
            raise self._error
        return self._mtime


class _SilentPluginSource:
    name = "quiet"

    async def run(self, context):   # pragma: no cover - contract only
        context.ok()


def _registry(*sources):
    return PluginRegistry((PluginSpec(name="acme", version="1.0", sources=sources),))


class DataLagTests(unittest.TestCase):
    """issue #39: a derived view that silently trails its sources is the
    failure mode that let a sibling tool sit 7 days stale."""

    def test_lag_is_artifact_minus_processed(self):
        entry = lag.SourceLag("claude", newest_artifact=500.0, newest_processed=200.0)
        self.assertEqual(entry.lag_s(), 300.0)
        self.assertTrue(entry.stale(299))
        self.assertFalse(entry.stale(300))

    def test_roster_ahead_of_mtimes_is_not_negative_lag(self):
        # A hook event lands before the transcript flush that follows it.
        entry = lag.SourceLag("claude", newest_artifact=100.0, newest_processed=180.0)
        self.assertEqual(entry.lag_s(), 0.0)
        self.assertFalse(entry.stale(0))

    def test_missing_half_reports_unknown_rather_than_stale(self):
        for entry in (lag.SourceLag("claude", newest_artifact=500.0),
                      lag.SourceLag("claude", newest_processed=500.0),
                      lag.SourceLag("claude")):
            self.assertIsNone(entry.lag_s())
            self.assertFalse(entry.stale(0))

    def test_newest_processed_is_per_source(self):
        processed = lag.newest_processed([
            {"key": "claude:1", "source": "claude", "last_activity": 100.0},
            {"key": "claude:2", "source": "claude", "last_activity": 400.0},
            {"key": "codex:a", "source": "codex", "last_activity": 250.0},
        ])
        self.assertEqual(processed, {"claude": 400.0, "codex": 250.0})

    def test_newest_processed_ignores_wsl_and_unusable_rows(self):
        # WSL reuses the claude/codex source names for another filesystem's
        # artifacts; folding it in would mask host-side lag.
        processed = lag.newest_processed([
            {"key": "wsl:claude:1", "source": "claude", "last_activity": 9_000.0},
            {"key": "claude:2", "source": "claude", "last_activity": 300.0},
            {"key": "codex:a", "source": "codex", "last_activity": 0},
            {"key": "codex:b", "source": "codex", "last_activity": True},
            {"key": "x:1", "source": None, "last_activity": 500.0},
            "not-a-session",
        ])
        self.assertEqual(processed, {"claude": 300.0})

    def test_newest_mtime_skips_paths_that_vanish(self):
        with tempfile.TemporaryDirectory() as tmp:
            present = Path(tmp) / "present.jsonl"
            present.write_text("{}")
            newest = lag.newest_mtime([Path(tmp) / "gone.jsonl", present])
            self.assertEqual(newest, present.stat().st_mtime)
            self.assertIsNone(lag.newest_mtime([Path(tmp) / "gone.jsonl"]))

    def test_collect_pairs_probes_with_processed_timestamps(self):
        entries = {e.source: e for e in lag.collect(
            {"claude": 100.0}, {"claude": lambda: 700.0})}
        self.assertEqual(entries["claude"].lag_s(), 600.0)
        self.assertIsNone(entries["claude"].detail)

    def test_collect_reports_a_source_with_no_live_artifacts(self):
        entry = lag.collect({"codex": 100.0}, {"codex": lambda: None})[0]
        self.assertEqual(entry.detail, "no live artifacts")
        self.assertFalse(entry.stale(0))

    def test_collect_does_not_warn_when_nothing_is_derived_yet(self):
        # The daemon deliberately hides sessions idle past ui.idle_ttl_s, so a
        # parked artifact with an empty roster is correct behaviour.
        entry = lag.collect({}, {"claude": lambda: 500.0})[0]
        self.assertEqual(entry.detail, "live artifacts, nothing derived yet")
        self.assertFalse(entry.stale(0))

    def test_collect_covers_probeless_and_uninstalled_sources(self):
        entries = {e.source: e for e in lag.collect(
            {"neo-cortex": 100.0, "gone": 100.0}, {"neo-cortex": None})}
        self.assertEqual(entries["neo-cortex"].detail,
                         "source does not report artifact times")
        self.assertEqual(entries["gone"].detail, "source is not installed")

    def test_collect_omits_self_timed_desktop_tiles(self):
        # Their derived timestamp *is* the mtime they read -- no gap to measure.
        entries = lag.collect({"claude-desktop": 100.0, "chatgpt-desktop": 100.0}, {})
        self.assertEqual(entries, [])

    def test_plugin_probe_is_used_when_offered(self):
        probes = lag.plugin_probes(_registry(_PluginSource(mtime=42.0)))
        self.assertEqual(probes["workers"](), 42.0)

    def test_plugin_without_probe_reports_none(self):
        probes = lag.plugin_probes(_registry(_SilentPluginSource()))
        self.assertIsNone(probes["quiet"])

    def test_broken_plugin_probe_degrades_to_unknown(self):
        probes = lag.plugin_probes(_registry(_PluginSource(error=RuntimeError("boom"))))
        self.assertIsNone(probes["workers"]())

    def test_plugin_probe_returning_junk_is_ignored(self):
        for value in ("later", None, True, object()):
            probes = lag.plugin_probes(_registry(_PluginSource(mtime=value)))
            self.assertIsNone(probes["workers"](), msg=value)


class NonFiniteLagTests(unittest.TestCase):
    """issue #41 M3: ``isinstance(x, (int, float))`` accepted nan/inf/-inf and
    absurd magnitudes. ``nan`` crashed doctor with ValueError and ``inf`` with
    OverflowError -- doctor is the tool you run *because* something is already
    wrong -- while ``-inf`` reported ``stale=False``, silently suppressing the
    staleness issue #39 exists to surface. ``1e300`` rendered a ~300-digit
    integer into the report."""

    def _probe(self, value):
        return lag.plugin_probes(_registry(_PluginSource(mtime=value)))["workers"]

    def test_every_non_finite_probe_value_reads_as_unknown(self):
        for value in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(value=value):
                self.assertIsNone(self._probe(value)())

    def test_an_absurd_magnitude_reads_as_unknown(self):
        # Not merely cosmetic: 1e300 also passed stale() and produced a
        # ~300-digit "seconds ago" figure.
        self.assertIsNone(self._probe(1e300)())
        self.assertIsNone(self._probe(-1.0)())

    def test_doctor_survives_a_non_finite_probe_instead_of_crashing(self):
        # The regression that matters: the whole report must still print.
        for value in (float("nan"), float("inf"), float("-inf"), 1e300):
            with self.subTest(value=value):
                probes = {"workers": self._probe(value)}
                entries = lag.collect({"workers": 100.0}, probes)
                self.assertEqual(entries[0].detail, "no live artifacts")
                self.assertFalse(entries[0].stale(0))
                lag.describe(entries[0], now=200.0)   # must not raise

    def test_negative_infinity_does_not_suppress_staleness(self):
        # The worst of the three: -inf made lag_s() clamp to 0.0 and report a
        # healthy source, which is the exact failure #39 exists to prevent.
        entry = lag.collect({"workers": 100.0}, {"workers": self._probe(float("-inf"))})[0]
        self.assertIsNone(entry.newest_artifact)
        self.assertIsNone(entry.lag_s())

    def test_a_probe_raising_system_exit_does_not_take_doctor_down(self):
        # issue #41 M2: _guarded caught Exception, not BaseException, so a probe
        # calling sys.exit() propagated out and violated this function's own
        # documented contract that one broken probe must not end the report.
        def exiting():
            raise SystemExit("a stray sys.exit in a plugin probe")

        self.assertIsNone(lag._guarded(exiting)())
        self.assertIsNone(lag._guarded(lambda: (_ for _ in ()).throw(KeyboardInterrupt()))())

    def test_a_non_finite_snapshot_timestamp_is_ignored(self):
        # A restored snapshot is a file on disk; an inf there breaks the same
        # arithmetic as one from a probe.
        processed = lag.newest_processed([
            {"key": "a:1", "source": "workers", "last_activity": float("inf")},
            {"key": "a:2", "source": "workers", "last_activity": float("nan")},
            {"key": "a:3", "source": "workers", "last_activity": 300.0},
            {"key": "a:4", "source": "other", "last_activity": 1e300},
        ])
        self.assertEqual(processed, {"workers": 300.0})

    def test_builtin_claude_probe_reads_live_status_files_and_transcripts(self):
        from huginn.model import Session
        with tempfile.TemporaryDirectory() as tmp:
            sessions_dir = Path(tmp) / "sessions"
            sessions_dir.mkdir()
            status = sessions_dir / "4242.json"
            status.write_text("{}")
            transcript = Path(tmp) / "abc.jsonl"
            transcript.write_text("{}")
            import os
            os.utime(status, (1_000.0, 1_000.0))
            os.utime(transcript, (2_000.0, 2_000.0))
            live = Session(key="claude:4242", source="claude", session_id="abc", cwd="",
                           name="abc", pid=4242, transcript_path=str(transcript))
            with patch("huginn.sources.claude_code.SESSIONS_DIR", sessions_dir), \
                    patch("huginn.sources.claude_code.scan", return_value=[live]):
                probe = lag.builtin_probes(config.Config({}))["claude"]
                self.assertEqual(probe(), 2_000.0)

    def test_builtin_claude_probe_ignores_dead_sessions(self):
        # A leftover status file for a dead PID is correctly skipped by the
        # source, so counting it would report lag for work that was never ours.
        with tempfile.TemporaryDirectory() as tmp:
            sessions_dir = Path(tmp) / "sessions"
            sessions_dir.mkdir()
            (sessions_dir / "9999.json").write_text("{}")
            with patch("huginn.sources.claude_code.SESSIONS_DIR", sessions_dir), \
                    patch("huginn.sources.claude_code.scan", return_value=[]):
                self.assertIsNone(lag.builtin_probes(config.Config({}))["claude"]())

    def test_builtin_codex_probe_reads_live_rollout_files(self):
        import os
        from huginn.model import Session
        with tempfile.TemporaryDirectory() as tmp:
            rollout = Path(tmp) / "rollout.jsonl"
            rollout.write_text("{}")
            os.utime(rollout, (3_000.0, 3_000.0))
            live = Session(key="codex:a", source="codex", session_id="a", cwd="",
                           name="a", transcript_path=str(rollout))
            untracked = Session(key="codex:b", source="codex", session_id="b", cwd="", name="b")
            cfg = config.Config({})
            with patch("huginn.sources.codex.scan", return_value=[live, untracked]) as scan:
                self.assertEqual(lag.builtin_probes(cfg)["codex"](), 3_000.0)
            scan.assert_called_once_with(cfg)

    def test_doctor_warns_past_the_configured_threshold(self):
        cfg = config.Config({"doctor": {"max_lag_s": 60.0}})
        now = time.time()
        sessions = [{"key": "claude:1", "source": "claude", "last_activity": now - 8 * 86400}]
        with patch("huginn.lag.builtin_probes",
                   return_value={"claude": lambda: now}), \
                patch("huginn.plugins.get_registry", return_value=PluginRegistry()), \
                _capture_stdout() as out:
            _report_data_lag(sessions, cfg)
        report = out.getvalue()
        self.assertIn("claude data lag", report)
        self.assertIn("over 60s threshold", report)
        self.assertIn("\033[33m!\033[0m", report)   # warn, not a hard failure

    def test_doctor_reports_a_fresh_source_as_ok(self):
        cfg = config.Config({})
        now = time.time()
        sessions = [{"key": "codex:a", "source": "codex", "last_activity": now - 3}]
        with patch("huginn.lag.builtin_probes",
                   return_value={"codex": lambda: now}), \
                patch("huginn.plugins.get_registry", return_value=PluginRegistry()), \
                _capture_stdout() as out:
            _report_data_lag(sessions, cfg)
        report = out.getvalue()
        self.assertIn("codex data lag", report)
        self.assertIn("\033[32m✓\033[0m", report)
        self.assertNotIn("threshold", report)

    def test_doctor_reports_plugin_sources_generically(self):
        cfg = config.Config({"doctor": {"max_lag_s": 60.0}})
        now = time.time()
        sessions = [{"key": "plugin:acme.workers:1", "source": "workers",
                     "last_activity": now - 7 * 86400}]
        with patch("huginn.lag.builtin_probes", return_value={}), \
                patch("huginn.plugins.get_registry",
                      return_value=_registry(_PluginSource(mtime=now))), \
                _capture_stdout() as out:
            _report_data_lag(sessions, cfg)
        self.assertIn("workers data lag", out.getvalue())
        self.assertIn("over 60s threshold", out.getvalue())

    def test_default_threshold_is_far_above_every_benign_gap(self):
        # Must not fire during normal operation (codex poll 5s, claude sweep
        # 10s, active-rollout window 240s, roster TTLs 300s) yet stay well
        # under Claude Code's 30-day cleanupPeriodDays deletion sweep.
        threshold = config.DEFAULTS["doctor"]["max_lag_s"]
        self.assertGreater(threshold, 10 * 300)
        self.assertLess(threshold, 86400)

    def test_snapshot_fallback_reads_the_persisted_roster(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            (state / "sessions.json").write_text(json.dumps({"sessions": {
                "claude:1": {"key": "claude:1", "source": "claude", "last_activity": 5.0},
            }}))
            with patch.object(config, "STATE_DIR", state):
                self.assertEqual(_snapshot_sessions(), [
                    {"key": "claude:1", "source": "claude", "last_activity": 5.0}])

    def test_snapshot_fallback_tolerates_missing_or_corrupt_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            with patch.object(config, "STATE_DIR", state):
                self.assertEqual(_snapshot_sessions(), [])
                (state / "sessions.json").write_text("{not json")
                self.assertEqual(_snapshot_sessions(), [])


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
