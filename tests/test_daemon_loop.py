"""The event loop must stay answerable while the daemon reads the machine.

The daemon serves HTTP on the same thread it polls from, so any read that
happens on the loop is time nobody's request is being answered. That was not
theoretical: reading four Claude status files took 10s on Windows, because each
session's shell count walked the process table through a PowerShell subprocess,
and Roost's two-second budget for /api/menu was missed by a daemon that was
otherwise idle.

Both halves of that are worth pinning, and they are different claims. The
speed of a single scan belongs to the platform adapter and is asserted in
tests/test_platform_windows.py. What is asserted here is the structural half,
which stays true however fast the reads get: a slow read does not stop the loop.
"""
from __future__ import annotations

import asyncio
import contextlib
import time
import unittest
from unittest.mock import patch

from huginn.config import Config
from huginn.daemon import Daemon


class ScansDoNotBlockTheLoop(unittest.TestCase):
    """Each case runs one slow read against a task that wants the loop.

    The reader records how much of the other task got to run *while it was
    still reading*. On the loop that number is zero by construction, since
    nothing else can run; off the loop it is not.
    """

    def _run(self, scan) -> int:
        progress: list[int] = []
        seen: dict[str, int] = {}

        def slow_read(*_args, **_kwargs):
            # The count either side of the sleep, not the total: the ticker has
            # already run a few times before the read starts, so a total is
            # non-zero even when the read blocks the loop completely. What is
            # being asked is whether anything ran *while* this was reading.
            before = len(progress)
            time.sleep(0.20)
            seen["during"] = len(progress) - before
            return []

        async def keep_ticking(stop: asyncio.Event) -> None:
            while not stop.is_set():
                progress.append(1)
                await asyncio.sleep(0.01)

        async def main() -> None:
            stop = asyncio.Event()
            ticker = asyncio.create_task(keep_ticking(stop))
            await asyncio.sleep(0.02)   # let the ticker start
            await scan(slow_read)
            stop.set()
            await ticker

        asyncio.run(main())
        return seen.get("during", 0)

    def test_reading_the_claude_status_files_leaves_the_loop_free(self):
        daemon = Daemon(Config({}))
        with patch("huginn.daemon.claude_code.SESSIONS_DIR") as directory:
            directory.is_dir.return_value = True

            async def scan(slow_read):
                with patch.object(daemon, "_read_claude_files", slow_read):
                    await daemon._scan_claude()

            self.assertGreater(
                self._run(scan), 0,
                "the loop was blocked for the whole status-file read",
            )

    @staticmethod
    async def _one_iteration(poller) -> None:
        """Run a polling loop long enough for one pass, then stop it."""
        task = asyncio.create_task(poller)
        await asyncio.sleep(0.30)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    def test_polling_codex_leaves_the_loop_free(self):
        daemon = Daemon(Config({}))

        async def scan(slow_read):
            with patch("huginn.daemon.codex.scan_with_status",
                       lambda _cfg: (slow_read(), True)), \
                 patch("huginn.daemon.codex.cli_terminal_alive", return_value=False):
                await self._one_iteration(daemon.codex_poller())

        self.assertGreater(
            self._run(scan), 0, "the loop was blocked for the whole Codex scan")

    def test_polling_for_a_desktop_app_leaves_the_loop_free(self):
        """Finding a running app is a process-table walk, on every tick."""
        daemon = Daemon(Config({}))

        async def scan(slow_read):
            with patch("huginn.sources.claude_desktop.scan",
                       lambda: slow_read() and None):
                await self._one_iteration(daemon.desktop_poller())

        self.assertGreater(
            self._run(scan), 0, "the loop was blocked for the whole app scan")


class TheScanIsStillTheScan(unittest.TestCase):
    """Moving the read must not change what the read reports."""

    def test_a_parsed_file_still_reaches_the_reducer(self):
        daemon = Daemon(Config({}))
        emitted: list[tuple] = []

        async def main():
            with patch("huginn.daemon.claude_code.SESSIONS_DIR") as directory, \
                 patch.object(daemon, "_read_claude_files",
                              return_value=[("p.json", {"pid": 1}, None)]), \
                 patch.object(daemon, "_emit_claude_parsed",
                              side_effect=lambda *a: emitted.append(a)):
                directory.is_dir.return_value = True
                await daemon._scan_claude()

        asyncio.run(main())
        self.assertEqual(emitted, [("p.json", {"pid": 1}, None)])

    def test_an_unreadable_file_is_skipped_rather_than_raised(self):
        daemon = Daemon(Config({}))
        path, raw, sess = daemon._read_claude_file(
            __import__("pathlib").Path("does-not-exist.json")
        )
        self.assertIsNone(raw)
        self.assertIsNone(sess)
        # And acting on that answer is a no-op rather than an AttributeError.
        daemon._emit_claude_parsed(path, raw, sess)
        self.assertTrue(daemon.bus.events.empty())


if __name__ == "__main__":
    unittest.main()
