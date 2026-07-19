"""File/dir permissions (0700/0600 regardless of umask) and bounded
notification-log retention, and chat digest cleanup on every exit path --
issue #24."""
from __future__ import annotations

import asyncio
import os
import stat
import tempfile
import time
import unittest
from pathlib import Path

from huginn import config
from huginn.config import Config
from huginn.daemon import Daemon
from huginn.model import Session, SessionState
from huginn.server import app as app_module
from huginn.server.app import NOTIFICATIONS_LOG_MAX_BYTES, _log_notification


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


class PermissiveUmaskTestCase(unittest.TestCase):
    """Everything here runs under a deliberately permissive umask -- the
    point is proving we chmod explicitly rather than relying on mkdir/open's
    mode= params, which umask silently masks."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_umask = os.umask(0o022)
        self._orig = {
            "STATE_DIR": config.STATE_DIR, "CACHE_DIR": config.CACHE_DIR,
            "CONFIG_DIR": config.CONFIG_DIR, "CONFIG_PATH": config.CONFIG_PATH,
            "TOKEN_PATH": config.TOKEN_PATH,
        }
        base = Path(self.tmp.name)
        config.STATE_DIR = base / "state"
        config.CACHE_DIR = base / "cache"
        config.CONFIG_DIR = base / "config"
        config.CONFIG_PATH = config.CONFIG_DIR / "config.toml"
        config.TOKEN_PATH = config.STATE_DIR / "token"
        # NOTIFICATIONS_LOG is computed once at import time from
        # config.STATE_DIR, so patching config.STATE_DIR alone doesn't
        # retarget it -- patch the module constant directly too.
        self._orig_notif_log = app_module.NOTIFICATIONS_LOG
        app_module.NOTIFICATIONS_LOG = config.STATE_DIR / "notifications.log"

    def tearDown(self):
        os.umask(self.old_umask)
        for name, value in self._orig.items():
            setattr(config, name, value)
        app_module.NOTIFICATIONS_LOG = self._orig_notif_log
        self.tmp.cleanup()


class DirPermissionTests(PermissiveUmaskTestCase):
    def test_ensure_state_dirs_is_0700(self):
        config.ensure_state_dirs()
        self.assertEqual(_mode(config.STATE_DIR), 0o700)
        self.assertEqual(_mode(config.CACHE_DIR), 0o700)

    def test_write_token_is_0600(self):
        config.write_token()
        self.assertEqual(_mode(config.TOKEN_PATH), 0o600)

    def test_save_config_is_0600_in_0700_dir(self):
        config.save(Config({}))
        self.assertEqual(_mode(config.CONFIG_DIR), 0o700)
        self.assertEqual(_mode(config.CONFIG_PATH), 0o600)


class SnapshotPermissionTests(PermissiveUmaskTestCase):
    def test_snapshot_is_0600(self):
        d = Daemon(Config({}))
        d._write_snapshot()
        self.assertEqual(_mode(d.SNAPSHOT_PATH), 0o600)


class NotificationLogTests(PermissiveUmaskTestCase):
    def test_log_is_0600(self):
        _log_notification("claude", "hello")
        self.assertEqual(_mode(config.STATE_DIR / "notifications.log"), 0o600)

    def test_rotation_bounds_growth(self):
        path = config.STATE_DIR / "notifications.log"
        config.ensure_state_dirs()
        path.write_text(("x" * 200 + "\n") * ((NOTIFICATIONS_LOG_MAX_BYTES // 200) + 100))
        self.assertGreater(path.stat().st_size, NOTIFICATIONS_LOG_MAX_BYTES)
        _log_notification("claude", "trigger rotation")
        self.assertLess(path.stat().st_size, NOTIFICATIONS_LOG_MAX_BYTES)


def _session() -> Session:
    return Session(key="claude:1", source="claude", session_id="s1", cwd="/tmp",
                   name="work", state=SessionState.DONE, state_since=time.time(),
                   state_origin="hook", last_activity=time.time())


class _StreamProvider:
    def __init__(self, chunks=None, exc=None, cancel=False, on_start=None):
        self.chunks = chunks or []
        self.exc = exc
        self.cancel = cancel
        self.on_start = on_start

    async def stream(self, *args, **kwargs):
        if self.on_start:
            self.on_start(kwargs.get("cwd"))
        for c in self.chunks:
            yield c
        if self.exc:
            raise self.exc
        if self.cancel:
            raise asyncio.CancelledError()


class ChatDigestCleanupTests(PermissiveUmaskTestCase):
    async def _run(self, provider):
        from huginn.llm.chat import _run_chat
        daemon = Daemon(Config({}))
        s = _session()
        daemon.reducer.sessions[s.key] = s
        await _run_chat(daemon, provider, "how's it going?")

    def test_chat_dir_removed_after_success(self):
        asyncio.run(self._run(_StreamProvider(chunks=["hi"])))
        self.assertFalse((config.CACHE_DIR / "chat").exists())

    def test_chat_dir_removed_after_provider_error(self):
        asyncio.run(self._run(_StreamProvider(exc=RuntimeError("boom"))))
        self.assertFalse((config.CACHE_DIR / "chat").exists())

    def test_chat_dir_removed_after_cancellation(self):
        with self.assertRaises(asyncio.CancelledError):
            asyncio.run(self._run(_StreamProvider(cancel=True)))
        self.assertFalse((config.CACHE_DIR / "chat").exists())

    def test_chat_digest_files_are_0600_in_0700_dir(self):
        captured = {}

        def on_start(cwd):
            d = Path(cwd)
            captured["mode_dir"] = _mode(d)
            captured["mode_files"] = [_mode(f) for f in d.iterdir()]

        asyncio.run(self._run(_StreamProvider(on_start=on_start)))
        self.assertEqual(captured["mode_dir"], 0o700)
        self.assertTrue(captured["mode_files"])
        self.assertTrue(all(m == 0o600 for m in captured["mode_files"]))


if __name__ == "__main__":
    unittest.main()
