"""Terminating signals must reach the daemon's teardown (issue #43).

The mistake this guards against is subtle enough to be worth naming: uvicorn
*does* handle SIGTERM, so the bug does not look like a missing handler. What it
does on the way out of ``capture_signals()`` is restore the previous handler and
re-raise the captured signal. For SIGINT that re-raise becomes
``KeyboardInterrupt``, which propagates *through* the ``finally`` in
``Daemon.run`` and the teardown runs. For SIGTERM the restored handler is the
default -- terminate now -- so the process dies inside uvicorn's own cleanup and
``daemon.json``, the token, and the raven descriptor are all left behind.

Installing our own handler before ``server.serve()`` is what breaks that chain:
uvicorn records ours as the handler to restore, so its re-raise lands on a
handler that only asks for shutdown, which has already happened.
"""
from __future__ import annotations

import asyncio
import signal
import unittest

from huginn import config
from huginn.daemon import Daemon


class _FakeServer:
    """Stands in for uvicorn.Server: only should_exit/force_exit matter here."""

    def __init__(self):
        self.should_exit = False
        self.force_exit = False


async def _let_handlers_run() -> None:
    """Give the loop a real iteration.

    ``asyncio.sleep(0)`` is not enough: a signal raised in-process is delivered
    to the loop through its self-pipe, so the callback only runs once the loop
    actually polls. Yielding zero times passes on a broken handler.
    """
    await asyncio.sleep(0.05)


class TerminationHandlerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.daemon = Daemon(config.Config({}))

    async def test_sigterm_requests_graceful_shutdown(self):
        server = _FakeServer()
        self.daemon._install_termination_handler(server)
        try:
            signal.raise_signal(signal.SIGTERM)
            await _let_handlers_run()
            self.assertTrue(server.should_exit)
            # force_exit would drop in-flight requests, which Ctrl-C never did.
            self.assertFalse(server.force_exit)
        finally:
            self._remove(signal.SIGTERM, signal.SIGHUP)

    @unittest.skipUnless(hasattr(signal, "SIGHUP"), "POSIX-only signal")
    async def test_sighup_also_requests_shutdown(self):
        # A daemon started from a terminal that then closes gets SIGHUP, and
        # losing the teardown there orphans exactly the same files.
        server = _FakeServer()
        self.daemon._install_termination_handler(server)
        try:
            signal.raise_signal(signal.SIGHUP)
            await _let_handlers_run()
            self.assertTrue(server.should_exit)
        finally:
            self._remove(signal.SIGTERM, signal.SIGHUP)

    async def test_sigint_is_left_to_uvicorn(self):
        # SIGINT already reached the teardown via KeyboardInterrupt. Claiming it
        # here would swap a working path for an untested one.
        loop = asyncio.get_running_loop()
        sentinel_ran = []
        loop.add_signal_handler(signal.SIGINT, lambda: sentinel_ran.append(True))
        try:
            self.daemon._install_termination_handler(_FakeServer())
            signal.raise_signal(signal.SIGINT)
            await _let_handlers_run()
            self.assertEqual(sentinel_ran, [True])
        finally:
            self._remove(signal.SIGINT, signal.SIGTERM, signal.SIGHUP)

    async def test_installing_twice_is_harmless(self):
        # A restart in one process re-enters run(); the second install must not
        # raise and must still shut the current server down.
        first, second = _FakeServer(), _FakeServer()
        self.daemon._install_termination_handler(first)
        self.daemon._install_termination_handler(second)
        try:
            signal.raise_signal(signal.SIGTERM)
            await _let_handlers_run()
            self.assertTrue(second.should_exit)
        finally:
            self._remove(signal.SIGTERM, signal.SIGHUP)

    def test_run_installs_the_handler_before_serving(self):
        """The handler existing is not the guarantee -- ``run()`` calling it is.

        Written after noticing the tests above all pass with the call site in
        ``run()`` deleted, because they exercise the helper directly. The
        original bug was not a broken handler, it was no handler at all, so the
        wiring needs its own assertion. Source inspection rather than a live
        daemon: booting one here would mean binding a port, restoring a
        snapshot, and starting every poller for a two-line ordering check.
        """
        import inspect

        from huginn import daemon as daemon_module

        source = inspect.getsource(daemon_module.Daemon.run)
        install_at = source.find("_install_termination_handler(")
        serve_at = source.find("await server.serve(")
        self.assertNotEqual(install_at, -1, "run() must install the termination handler")
        self.assertNotEqual(serve_at, -1, "run() must still await server.serve()")
        # Order matters: uvicorn only records ours as the handler to restore if
        # it is already installed when capture_signals() runs inside serve().
        self.assertLess(install_at, serve_at,
                        "the handler must be installed before serve() captures signals")

    @staticmethod
    def _remove(*sigs) -> None:
        loop = asyncio.get_event_loop()
        for sig in sigs:
            if getattr(signal, sig.name, None) is None:   # pragma: no cover
                continue
            try:
                loop.remove_signal_handler(sig)
            except (NotImplementedError, RuntimeError, ValueError):   # pragma: no cover
                pass


if __name__ == "__main__":
    unittest.main()
