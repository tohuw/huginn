"""The daemon.json contract (issue #37). The file records which interpreter and
root the daemon ran from, because a tray app that found no live daemon had to
relaunch one and the macOS menu bar used to hardcode a developer's checkout
instead.

Both of those trays are now deleted and their replacement (Roost) starts nothing,
so **no code in this repository executes "python" any more.** The tests below are
kept as-is regardless, for two reasons worth stating so nobody relaxes them: the
file is still read by huginn/cli.py and huginn/doctor.py off pid/port/started, and
the 0600 mode should not be loosened just because its most dangerous reader left.
A file whose permissions track whoever happens to read it today is a file that is
0644 when the next reader arrives."""
from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

from huginn import config
from huginn.config import Config
from huginn.daemon import Daemon


class DaemonStateFileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._orig_state_dir = config.STATE_DIR
        config.STATE_DIR = Path(self.tmp.name)

    def tearDown(self):
        config.STATE_DIR = self._orig_state_dir
        self.tmp.cleanup()

    def _write(self, port: int = 47100) -> dict:
        Daemon(Config({}))._write_daemon_state(port)
        return json.loads((config.STATE_DIR / "daemon.json").read_text())

    def test_keeps_the_original_keys(self):
        state = self._write(port=47123)

        self.assertEqual(state["port"], 47123)
        self.assertIsInstance(state["pid"], int)
        self.assertIsInstance(state["started"], float)
        self.assertEqual((config.STATE_DIR / "port").read_text(), "47123")

    def test_records_a_runnable_interpreter(self):
        state = self._write()

        self.assertEqual(state["python"], sys.executable)
        self.assertTrue(Path(state["python"]).exists())

    def test_records_a_root_holding_the_huginn_package(self):
        state = self._write()

        # What the tray passes as cwd for `python -m huginn.cli serve`, so it
        # has to be the directory the package sits in, not the package itself.
        self.assertTrue((Path(state["repo"]) / "huginn" / "cli.py").exists())

    def test_carries_nothing_secret(self):
        # Still holds no secret -- but see the mode test below: the 0600 there
        # is about integrity, not confidentiality.
        daemon = Daemon(Config({}))
        daemon.token = "secret-token"
        daemon.refresh_token = "secret-refresh-token"
        daemon._write_daemon_state(47100)
        raw = (config.STATE_DIR / "daemon.json").read_text()

        self.assertNotIn("secret-token", raw)
        self.assertNotIn("secret-refresh-token", raw)
        self.assertEqual(
            set(json.loads(raw)), {"pid", "port", "started", "python", "repo"})

    @unittest.skipIf(os.name == "nt", "POSIX mode bits do not model Windows ACLs")
    def test_is_not_group_or_world_writable(self):
        # issue #41 M5: written with a bare write_text, so 0644 at the default
        # umask, unlike its 0600 siblings (token, sessions.json). It holds no
        # secret, but the (now deleted) macos/HuginnMenuBar.swift *executed* the
        # "python" path from it, so integrity mattered where confidentiality did
        # not -- and only the 0700 parent stood between that and any process able
        # to write here. Still asserted with the executor gone: see the module
        # docstring.
        self._write()
        mode = stat.S_IMODE((config.STATE_DIR / "daemon.json").stat().st_mode)

        self.assertEqual(mode & (stat.S_IWGRP | stat.S_IWOTH), 0, oct(mode))
        self.assertEqual(mode, 0o600, oct(mode))

    @unittest.skipIf(os.name == "nt", "POSIX mode bits do not model Windows ACLs")
    def test_rewriting_keeps_the_restricted_mode(self):
        # A restart must not silently widen it back to 0644.
        self._write()
        self._write(port=47201)
        mode = stat.S_IMODE((config.STATE_DIR / "daemon.json").stat().st_mode)

        self.assertEqual(mode, 0o600, oct(mode))


if __name__ == "__main__":
    unittest.main()
