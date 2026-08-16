"""The shared raven descriptor directory, write, withdraw, and liveness (#42).

The resolution rule in :func:`corvidae.state_dir` is the reason this is shared
code rather than a documented convention: every participant, host included, must
resolve the same directory, and when two disagree the failure is *silent* -- a
raven that published where the host is not looking is indistinguishable from a
raven that was never installed. So these tests are largely about the location.
"""
from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from corvidae import (
    descriptor_is_live,
    descriptor_path,
    publish_descriptor,
    read_descriptor,
    state_dir,
    withdraw_descriptor,
)

PAYLOAD = {"name": "testraven", "pid": 4321, "port": 47100, "started": 1_000.0}

# NTFS does not honour mode bits, and corvidae takes no pywin32 dependency to set
# a DACL instead, so ``descriptor._restrict`` is a documented no-op on Windows.
# These assert that mechanism, so they run where the mechanism exists. Skipping
# is honest here in a way that relaxing the assertion would not be: a weakened
# check would still be green on POSIX, where the guarantee is real and worth
# defending exactly.
posix_modes_only = unittest.skipUnless(
    os.name != "nt", "POSIX mode bits do not model Windows ACLs"
)


class StateDirTests(unittest.TestCase):
    """The resolution order *is* the contract."""

    def setUp(self):
        self._env = {k: os.environ.get(k)
                     for k in ("RAVENS_STATE_DIR", "XDG_STATE_HOME", "LOCALAPPDATA")}
        for key in self._env:
            os.environ.pop(key, None)

    def tearDown(self):
        for key, value in self._env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_posix_default(self):
        with patch.object(sys, "platform", "darwin"):
            self.assertEqual(state_dir(), Path.home() / ".local" / "state" / "ravens")

    def test_posix_honours_xdg_state_home(self):
        # Not optional even where a consumer's own state dir ignores it: this
        # directory is shared with the host and every other raven.
        os.environ["XDG_STATE_HOME"] = "/tmp/xdg"
        with patch.object(sys, "platform", "linux"):
            self.assertEqual(state_dir(), Path("/tmp/xdg/ravens"))

    def test_an_empty_xdg_state_home_is_treated_as_unset(self):
        # Exported-but-empty is common in shell profiles, and Path("")/"ravens"
        # would resolve to a relative "ravens" in the cwd.
        os.environ["XDG_STATE_HOME"] = "   "
        with patch.object(sys, "platform", "linux"):
            self.assertEqual(state_dir(), Path.home() / ".local" / "state" / "ravens")

    def test_windows_uses_localappdata(self):
        os.environ["LOCALAPPDATA"] = r"C:\Users\me\AppData\Local"
        with patch.object(sys, "platform", "win32"):
            self.assertEqual(state_dir(), Path(r"C:\Users\me\AppData\Local") / "Ravens")

    def test_windows_falls_back_without_localappdata(self):
        with patch.object(sys, "platform", "win32"):
            self.assertEqual(state_dir(), Path.home() / "AppData" / "Local" / "Ravens")

    def test_windows_capitalisation_differs_from_posix_deliberately(self):
        # "Ravens" on Windows, "ravens" on POSIX. That is the host's own rule and
        # matching it matters more than internal consistency.
        os.environ["LOCALAPPDATA"] = r"C:\L"
        with patch.object(sys, "platform", "win32"):
            self.assertEqual(state_dir().name, "Ravens")
        with patch.object(sys, "platform", "linux"):
            self.assertEqual(state_dir().name, "ravens")

    def test_explicit_override_wins_on_every_platform(self):
        os.environ["RAVENS_STATE_DIR"] = "/tmp/override"
        os.environ["XDG_STATE_HOME"] = "/tmp/xdg"
        os.environ["LOCALAPPDATA"] = r"C:\L"
        for platform in ("darwin", "win32", "linux"):
            with self.subTest(platform=platform), patch.object(sys, "platform", platform):
                self.assertEqual(state_dir(), Path("/tmp/override"))

    def test_the_override_expands_a_tilde(self):
        os.environ["RAVENS_STATE_DIR"] = "~/ravens-elsewhere"
        self.assertEqual(state_dir(), Path.home() / "ravens-elsewhere")

    def test_it_is_read_per_call_not_cached_at_import(self):
        # A cached value would ignore an override set after import, which is
        # exactly how a test harness (or a user) points every participant
        # somewhere else.
        os.environ["RAVENS_STATE_DIR"] = "/tmp/one"
        self.assertEqual(state_dir(), Path("/tmp/one"))
        os.environ["RAVENS_STATE_DIR"] = "/tmp/two"
        self.assertEqual(state_dir(), Path("/tmp/two"))

    def test_descriptor_path_is_the_raven_name_dot_json(self):
        os.environ["RAVENS_STATE_DIR"] = "/tmp/r"
        self.assertEqual(descriptor_path("muninn"), Path("/tmp/r/muninn.json"))


class PublishTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name) / "ravens"

    def tearDown(self):
        self.tmp.cleanup()

    def test_writes_the_payload_verbatim(self):
        path = publish_descriptor("testraven", PAYLOAD, directory=self.dir)
        self.assertEqual(path, self.dir / "testraven.json")
        self.assertEqual(json.loads(path.read_text()), PAYLOAD)

    @posix_modes_only
    def test_is_owner_only(self):
        # No secret in it, but another process reads a port (and maybe a token
        # path) out of it and acts on them: integrity matters where
        # confidentiality does not.
        path = publish_descriptor("testraven", PAYLOAD, directory=self.dir)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    @posix_modes_only
    def test_directory_is_created_owner_only(self):
        publish_descriptor("testraven", PAYLOAD, directory=self.dir)
        self.assertEqual(stat.S_IMODE(self.dir.stat().st_mode), 0o700)

    @posix_modes_only
    def test_an_existing_shared_directory_is_not_retightened(self):
        # Shared with other ravens; silently changing another project's directory
        # mode is not ours to do.
        self.dir.mkdir(parents=True)
        self.dir.chmod(0o755)
        publish_descriptor("testraven", PAYLOAD, directory=self.dir)
        self.assertEqual(stat.S_IMODE(self.dir.stat().st_mode), 0o755)

    def test_republishing_keeps_the_restricted_mode(self):
        publish_descriptor("testraven", PAYLOAD, directory=self.dir)
        path = publish_descriptor("testraven", {**PAYLOAD, "port": 47201},
                                  directory=self.dir)
        # The rewrite itself is the point and is checked everywhere; only the
        # mode half of it is POSIX-only.
        self.assertEqual(json.loads(path.read_text())["port"], 47201)
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_leaves_no_temp_file_behind(self):
        publish_descriptor("testraven", PAYLOAD, directory=self.dir)
        self.assertEqual([p.name for p in self.dir.iterdir()], ["testraven.json"])

    def test_a_failed_replace_leaves_no_temp_file_behind(self):
        from corvidae import descriptor as module
        with patch.object(module.os, "replace", side_effect=OSError("read-only")):
            with self.assertRaises(OSError):
                publish_descriptor("testraven", PAYLOAD, directory=self.dir)
        self.assertEqual(list(self.dir.iterdir()), [])

    def test_two_ravens_share_one_directory(self):
        publish_descriptor("huginn", {**PAYLOAD, "name": "huginn"}, directory=self.dir)
        publish_descriptor("muninn", {**PAYLOAD, "name": "muninn"}, directory=self.dir)
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()),
                         ["huginn.json", "muninn.json"])

    def test_the_json_is_sorted_and_newline_terminated(self):
        # Stable bytes for the same payload, so a diff of the file is meaningful
        # and a host reading it mid-write cannot see reordered keys.
        path = publish_descriptor("testraven", PAYLOAD, directory=self.dir)
        text = path.read_text()
        self.assertTrue(text.endswith("\n"))
        self.assertEqual(list(json.loads(text)), sorted(PAYLOAD))

    def test_defaults_to_the_shared_state_dir(self):
        with patch.dict(os.environ, {"RAVENS_STATE_DIR": str(self.dir)}):
            path = publish_descriptor("testraven", PAYLOAD)
        self.assertEqual(path, self.dir / "testraven.json")


class WithdrawTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name) / "ravens"

    def tearDown(self):
        self.tmp.cleanup()

    def test_removes_our_own_descriptor(self):
        publish_descriptor("testraven", {**PAYLOAD, "pid": os.getpid()}, directory=self.dir)
        self.assertTrue(withdraw_descriptor("testraven", directory=self.dir))
        self.assertFalse((self.dir / "testraven.json").exists())

    def test_leaves_another_processes_descriptor_alone(self):
        # A raven that lost a port race, or a replacement that already
        # republished, must not have its descriptor deleted by our exit.
        path = publish_descriptor("testraven", {**PAYLOAD, "pid": os.getpid() + 1},
                                  directory=self.dir)
        self.assertFalse(withdraw_descriptor("testraven", directory=self.dir))
        self.assertTrue(path.exists())

    def test_an_explicit_pid_is_honoured(self):
        publish_descriptor("testraven", {**PAYLOAD, "pid": 999}, directory=self.dir)
        self.assertTrue(withdraw_descriptor("testraven", pid=999, directory=self.dir))

    def test_is_quiet_when_nothing_was_published(self):
        # Shutdown runs this unconditionally, so it must never raise.
        self.assertFalse(withdraw_descriptor("testraven", directory=self.dir))

    def test_survives_a_corrupt_descriptor_and_leaves_it_in_place(self):
        # Left in place rather than guessed at: deleting a file we cannot prove is
        # ours is the thing this function exists not to do, and the host refuses an
        # unparseable descriptor with a visible reason anyway.
        self.dir.mkdir(parents=True)
        path = self.dir / "testraven.json"
        path.write_text("{not json")
        self.assertFalse(withdraw_descriptor("testraven", directory=self.dir))
        self.assertTrue(path.exists())

    def test_survives_a_descriptor_that_is_not_an_object(self):
        self.dir.mkdir(parents=True)
        (self.dir / "testraven.json").write_text("[1, 2]")
        self.assertFalse(withdraw_descriptor("testraven", directory=self.dir))


class ReadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name) / "ravens"

    def tearDown(self):
        self.tmp.cleanup()

    def test_round_trips_a_published_descriptor(self):
        publish_descriptor("testraven", PAYLOAD, directory=self.dir)
        self.assertEqual(read_descriptor("testraven", directory=self.dir), PAYLOAD)

    def test_absent_and_malformed_both_read_as_none(self):
        self.assertIsNone(read_descriptor("testraven", directory=self.dir))
        self.dir.mkdir(parents=True)
        (self.dir / "testraven.json").write_text("{oops")
        self.assertIsNone(read_descriptor("testraven", directory=self.dir))

    def test_a_non_object_document_reads_as_none(self):
        self.dir.mkdir(parents=True)
        (self.dir / "testraven.json").write_text('"a string"')
        self.assertIsNone(read_descriptor("testraven", directory=self.dir))


class LivenessTests(unittest.TestCase):
    """The host's own rule, so a `doctor` command can predict what it will say."""

    @staticmethod
    def _alive(pid: int) -> bool:
        return pid == 4321

    def test_a_live_pid_with_a_matching_start_time_is_live(self):
        self.assertTrue(descriptor_is_live(
            PAYLOAD, pid_alive=self._alive, process_start_time=lambda _p: 1_000.0))

    def test_a_dead_pid_is_not_live(self):
        self.assertFalse(descriptor_is_live({**PAYLOAD, "pid": 9999},
                                            pid_alive=self._alive))

    def test_a_recycled_pid_is_caught_by_the_start_time_cross_check(self):
        # The half that matters: a live process at the recorded pid is not on its
        # own evidence that it is the raven that wrote the file.
        self.assertFalse(descriptor_is_live(
            PAYLOAD, pid_alive=self._alive, process_start_time=lambda _p: 50_000.0))

    def test_a_start_time_within_the_slack_still_counts(self):
        # A raven reads its start time a moment after the process began, so a
        # strict comparison would report every live raven as recycled.
        self.assertTrue(descriptor_is_live(
            PAYLOAD, pid_alive=self._alive, process_start_time=lambda _p: 1_001.5))
        self.assertFalse(descriptor_is_live(
            PAYLOAD, pid_alive=self._alive, process_start_time=lambda _p: 1_010.0))

    def test_a_platform_that_cannot_answer_leaves_the_pid_check_standing(self):
        # A missing cross-check must not turn a live raven into a dead one: telling
        # the user nothing is running while it is, is the worse failure.
        for answer in (None, 0, 0.0):
            with self.subTest(answer=answer):
                self.assertTrue(descriptor_is_live(
                    PAYLOAD, pid_alive=self._alive, process_start_time=lambda _p: answer))

    def test_an_inspection_that_raises_leaves_the_pid_check_standing(self):
        def boom(_pid):
            raise OSError("no /proc")

        self.assertTrue(descriptor_is_live(
            PAYLOAD, pid_alive=self._alive, process_start_time=boom))

    def test_process_start_time_is_optional(self):
        self.assertTrue(descriptor_is_live(PAYLOAD, pid_alive=self._alive))

    def test_a_missing_or_unusable_started_is_treated_as_absent(self):
        # How a producer that could not read a real start time records "unknown".
        # Comparing against it would fail for every live process.
        for started in (None, 0, -1, "1000", True):
            with self.subTest(started=started):
                self.assertTrue(descriptor_is_live(
                    {**PAYLOAD, "started": started}, pid_alive=self._alive,
                    process_start_time=lambda _p: 1_000.0))

    def test_a_missing_or_unusable_pid_is_not_live(self):
        for pid in (None, "4321", 0, -1, True, 4321.0):
            with self.subTest(pid=pid):
                self.assertFalse(descriptor_is_live({**PAYLOAD, "pid": pid},
                                                    pid_alive=self._alive))

    def test_none_and_non_dicts_are_not_live(self):
        for payload in (None, [], "x", 7):
            with self.subTest(payload=payload):
                self.assertFalse(descriptor_is_live(payload, pid_alive=self._alive))

    def test_the_slack_is_adjustable(self):
        self.assertTrue(descriptor_is_live(
            PAYLOAD, pid_alive=self._alive,
            process_start_time=lambda _p: 1_010.0, slack=30.0))


if __name__ == "__main__":
    unittest.main()
