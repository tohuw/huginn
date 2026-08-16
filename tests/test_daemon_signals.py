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
import os
import signal
import unittest
import unittest.mock

from huginn import config
from huginn.daemon import Daemon

# asyncio's add_signal_handler is POSIX-only; on Windows it raises
# NotImplementedError, and ``Daemon._install_termination_handler`` documents that
# a missing handler is *correct* there because the tray owns lifecycle.
#
# Skipped rather than merely expected to fail: with no handler installed,
# ``signal.raise_signal(SIGTERM)`` runs the default action, which terminates the
# process running the tests. Unguarded, these do not fail the suite -- they kill
# it mid-run, taking every later test's result with them, which is how this went
# unnoticed. The wiring assertion below inspects source instead of raising
# anything, so it stays live on every OS.
posix_signals_only = unittest.skipIf(
    os.name == "nt", "asyncio signal handlers are POSIX-only; the tray owns "
                     "lifecycle on Windows (see WINDOWS.md)"
)


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

    @posix_signals_only
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

    @posix_signals_only
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

    @posix_signals_only
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


class MenuRequestedShutdownTests(unittest.TestCase):
    """``request_stop`` is the seam a menu-bar Quit/Restart row reaches.

    It exists so a menu click takes the *same* path a signal does rather than a
    second stop path of its own -- which is what keeps the teardown above the only
    teardown there is.
    """

    def setUp(self):
        self.daemon = Daemon(config.Config({}))

    def test_it_asks_for_the_same_graceful_shutdown_a_signal_does(self):
        server = _FakeServer()
        self.daemon._server = server

        self.assertTrue(self.daemon.request_stop())

        self.assertTrue(server.should_exit)
        # force_exit would drop the in-flight response that tells the menu bar the
        # quit was accepted, and skip the drain SIGTERM already gets.
        self.assertFalse(server.force_exit)

    def test_a_restart_records_the_comeback_before_setting_the_flag(self):
        """Order matters: once should_exit is set the loop may unwind at once.

        Setting the flag first and the intent second would race -- the exit path
        could read ``_restart_requested`` before it was written and quit instead of
        restarting.
        """
        import inspect

        source = inspect.getsource(Daemon.request_stop)
        recorded_at = source.find("_restart_requested =")
        flagged_at = source.find("should_exit = True")
        self.assertNotEqual(recorded_at, -1)
        self.assertNotEqual(flagged_at, -1)
        self.assertLess(recorded_at, flagged_at)

    def test_restart_and_quit_are_distinguishable_to_the_exit_path(self):
        self.daemon._server = _FakeServer()
        self.assertTrue(self.daemon.request_stop(restart=True))
        self.assertTrue(self.daemon._restart_requested)

        other = Daemon(config.Config({}))
        other._server = _FakeServer()
        self.assertTrue(other.request_stop(restart=False))
        self.assertFalse(other._restart_requested)

    def test_it_refuses_when_there_is_no_server(self):
        """A daemon that is not serving reports that rather than faking success."""
        self.assertIsNone(self.daemon._server)
        self.assertFalse(self.daemon.request_stop())
        self.assertFalse(self.daemon.request_stop(restart=True))
        # And a refusal must not leave a restart armed for a later run.
        self.assertFalse(self.daemon._restart_requested)

    def test_it_never_signals_or_exits(self):
        """The stop is a flag, not a kill.

        The superseded macOS app sent SIGTERM and escalated to SIGKILL after a
        second, which is how the descriptor, ``daemon.json``, and the token got
        orphaned. Nothing in this path may reach a signal or an exit.
        """
        import ast
        import inspect
        import textwrap

        # The *code*, not the prose: the docstring names SIGKILL deliberately, to
        # say what this path must not do. Walking the AST for calls is what
        # distinguishes "explains the hazard" from "is the hazard".
        tree = ast.parse(textwrap.dedent(inspect.getsource(Daemon.request_stop)))
        called = {
            ast.unparse(node.func)
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
        }
        for forbidden in ("os.kill", "kill", "signal.raise_signal", "sys.exit",
                          "os._exit", "server.terminate", "exit"):
            self.assertNotIn(forbidden, called)
        # And no signal constant is *used* as a value anywhere in the body.
        attributes = {
            ast.unparse(node)
            for node in ast.walk(tree)
            if isinstance(node, (ast.Attribute, ast.Name))
        }
        for forbidden in ("signal.SIGTERM", "signal.SIGKILL", "SIGKILL"):
            self.assertNotIn(forbidden, attributes)


class RestartLoopTests(unittest.TestCase):
    """``daemon.run`` serves again when a Restart was requested, and not otherwise."""

    def test_a_quit_returns_instead_of_serving_again(self):
        from huginn import daemon as daemon_module

        starts = []

        async def once(self, open_browser=True):
            starts.append(open_browser)
            return 0

        with unittest.mock.patch.object(daemon_module.Daemon, "run", once):
            result = daemon_module.run(config.Config({}), open_browser=False)

        self.assertEqual(result, 0)
        self.assertEqual(len(starts), 1)

    def test_a_restart_serves_again_with_a_fresh_daemon(self):
        """A restart must look like a restart to everything watching it.

        A new ``Daemon`` per iteration means a new ``boot_id`` -- which is how the
        dashboard tells "still the daemon I was talking to" from "it came back" --
        and a clean reducer. Reusing the instance would also carry the previous
        run's ``_restart_requested`` and loop forever.
        """
        from huginn import daemon as daemon_module

        boot_ids = []
        browser_flags = []

        async def serve_then_stop(self, open_browser=True):
            boot_ids.append(self.boot_id)
            browser_flags.append(open_browser)
            # Restart on the first pass only, then exit for real.
            self._restart_requested = len(boot_ids) == 1
            return 0

        with unittest.mock.patch.object(daemon_module.Daemon, "run", serve_then_stop):
            result = daemon_module.run(config.Config({}), open_browser=True)

        self.assertEqual(result, 0)
        self.assertEqual(len(boot_ids), 2)
        # Fresh instance, so a fresh boot id: the dashboard can see the restart.
        self.assertNotEqual(boot_ids[0], boot_ids[1])
        # A restart is not a first launch, so no second browser tab.
        self.assertEqual(browser_flags, [True, False])

    def test_a_port_conflict_still_reports_rather_than_looping(self):
        """The pre-existing "address already in use" path must not become a spin."""
        from huginn import daemon as daemon_module

        attempts = []

        async def busy(self, open_browser=True):
            attempts.append(1)
            raise OSError("address already in use")

        with unittest.mock.patch.object(daemon_module.Daemon, "run", busy):
            result = daemon_module.run(config.Config({}), open_browser=False)

        self.assertEqual(result, 1)
        self.assertEqual(len(attempts), 1)

    def test_keyboard_interrupt_still_exits_zero(self):
        from huginn import daemon as daemon_module

        async def interrupted(self, open_browser=True):
            raise KeyboardInterrupt

        with unittest.mock.patch.object(daemon_module.Daemon, "run", interrupted):
            self.assertEqual(daemon_module.run(config.Config({}), open_browser=False), 0)


if __name__ == "__main__":
    unittest.main()
