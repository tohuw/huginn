"""The daemon: wires sources → bus → reducer → SSE server."""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import socket
import sys
import time
import uuid
import webbrowser
from pathlib import Path

from . import config, raven
from .bus import Bus
from .diagnostics import Diagnostics
from .model import Event, Session, SessionState
from .plugins import SourceContext, get_registry
from .sources import claude_code, codex
from .sources.transcript import ClaudeAnalyzer, CodexAnalyzer, Tail
from .state import Reducer
from .steering import ConfirmationStore
from .triage import build_triage

LOG = logging.getLogger("huginn.daemon")


class Daemon:
    def __init__(self, cfg: config.Config):
        self.cfg = cfg
        # Fresh per-process-start id, unrelated to the API token: exposed to
        # the dashboard so it can tell "still talking to the daemon that had
        # this conversation" apart from "a new daemon started" (restart or
        # quit) without that distinction depending on session/window state.
        self.boot_id = uuid.uuid4().hex
        self.bus = Bus()
        self.reducer = Reducer(cfg)
        # session key -> (Tail, analyzer)
        self.tails: dict[str, tuple[Tail, ClaudeAnalyzer | CodexAnalyzer]] = {}
        self._last_attention = -1
        self._dirty = False   # sessions/hook_hits changed since the last snapshot write
        self.token = ""   # set for real in run(); tests may set it directly
        self.refresh_token = ""  # persistent; lets authorized browser tabs recover
        self.hook_hits: dict[str, int] = {}   # "{source}.{event}" -> count, issue #2
        # A database roster can omit an open CLI thread because it crossed a
        # recency window or row cap.  Require repeated misses before removal.
        self._codex_missing_polls: dict[str, int] = {}
        # Owned here (not a chat.py module global) so multiple Daemon
        # instances in one process never share chat state -- issue #17.
        self.active_chat: asyncio.Task | None = None
        self.steering_confirmations = ConfirmationStore()
        self.diagnostics = Diagnostics()   # issue #15
        # The uvicorn server, once run() has one. Held so a menu-bar lifecycle
        # action can ask for the *same* graceful shutdown a signal gets (issue
        # #43) rather than a second, harder stop path of its own. None until
        # serving, which is why request_stop() refuses instead of assuming.
        self._server = None
        #: Set by a Restart action so the exit path knows to come back up. Read
        #: after asyncio.run() returns, where the teardown has already withdrawn
        #: the descriptor, the state file, and the token.
        self._restart_requested = False
        self.plugins = get_registry()
        from .llm.blurb import BlurbWorker
        self.blurbs = BlurbWorker(self)

    def mark_dirty(self) -> None:
        self._dirty = True

    def record_hook_hit(self, source: str, event: str) -> None:
        key = f"{source}.{event}"
        self.hook_hits[key] = self.hook_hits.get(key, 0) + 1
        self.mark_dirty()

    # ---------------------------------------------------------- persistence
    SNAPSHOT_PATH = property(lambda self: config.STATE_DIR / "sessions.json")

    def _restore_snapshot(self) -> None:
        try:
            data = json.loads(self.SNAPSHOT_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        self.reducer.restore(data.get("sessions", {}))
        active_plugin_prefixes = tuple(
            f"plugin:{plugin.name}.{source.name}:"
            for plugin, source in self.plugins.sources()
        )
        for key in list(self.reducer.sessions):
            if key.startswith("plugin:") and not key.startswith(active_plugin_prefixes):
                del self.reducer.sessions[key]
        self.hook_hits = data.get("hook_hits", {})

    def _write_snapshot(self) -> None:
        # Contains prompt/blurb text -- 0600 regardless of umask (issue #24).
        config.ensure_state_dirs()
        data = json.dumps({"sessions": self.reducer.snapshot(), "hook_hits": self.hook_hits})
        tmp = self.SNAPSHOT_PATH.with_suffix(".json.tmp")
        tmp.write_text(data, encoding="utf-8")
        tmp.chmod(0o600)
        os.replace(tmp, self.SNAPSHOT_PATH)

    def _flush_snapshot_if_dirty(self) -> bool:
        """Persist pending state, retaining the dirty bit for a later retry."""
        if not self._dirty:
            return True
        try:
            self._write_snapshot()
            self.diagnostics.ok("snapshot")
        except OSError as e:
            self.diagnostics.error("snapshot", e)
            return False
        self._dirty = False
        return True

    # ------------------------------------------------------------ tail mgmt
    def ensure_tail(self, s: Session) -> None:
        if not s.transcript_path or s.key in self.tails:
            return
        if not Path(s.transcript_path).exists():
            return
        tail = Tail(s.transcript_path)
        analyzer = ClaudeAnalyzer() if s.source == "claude" else CodexAnalyzer()
        entries = tail.attach()
        analyzer.feed(entries)
        self.tails[s.key] = (tail, analyzer)
        kind = "transcript.activity" if s.source == "claude" else "codex.activity"
        payload = analyzer.activity()
        payload["live"] = False   # seed data; not evidence of current work
        self.bus.emit(Event(kind, s.key, time.time(), "transcript", payload))

    def _recent_turn_end(self, key: str, status_since: float = 0) -> bool:
        pair = self.tails.get(key)
        if not pair:
            return False
        _, an = pair
        if not isinstance(an, ClaudeAnalyzer):
            return False
        return an.last_entry_type == "assistant" and not an.pending_tools

    # ------------------------------------------------------------- watchers
    async def claude_watcher(self) -> None:
        from watchfiles import awatch
        self._scan_claude()
        sweep = asyncio.create_task(self._claude_sweep())
        try:
            if claude_code.SESSIONS_DIR.is_dir():
                async for changes in awatch(claude_code.SESSIONS_DIR, recursive=False):
                    for change, path in changes:
                        p = Path(path)
                        if p.suffix != ".json":
                            continue
                        if not p.exists():
                            key = f"claude:{p.stem}"
                            sess = self.reducer.sessions.get(key)
                            if sess is not None and (sess.pid is None
                                                     or not claude_code.pid_alive(sess.pid)):
                                self.bus.emit(Event("claude.dead", key,
                                                    time.time(), "statusfile"))
                        else:
                            self._emit_claude_file(p)
        finally:
            sweep.cancel()

    def _scan_claude(self) -> None:
        if claude_code.SESSIONS_DIR.is_dir():
            for p in claude_code.SESSIONS_DIR.glob("*.json"):
                self._emit_claude_file(p)

    def _emit_claude_file(self, path: Path) -> None:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        sess = claude_code.parse_session_file(path)
        if sess is None:
            return
        # VS Code keeps one Claude backend alive for days. Once an idle
        # session has aged out it is real, but no longer useful in a live
        # attention roster. Remove an already-known card and suppress re-adds
        # until the status file reports fresh activity.
        if (sess.entrypoint != "cli"
                and sess.state == SessionState.IDLE
                and time.time() - sess.last_activity >= self.cfg.get("ui", "idle_ttl_s")):
            if sess.key in self.reducer.sessions:
                self.bus.emit(Event("session.hide", sess.key, time.time(), "timeout"))
            return
        if not (claude_code.pid_alive(sess.pid)
                and claude_code.pid_matches_start(sess.pid, raw.get("procStart"))):
            if sess.key in self.reducer.sessions:
                self.bus.emit(Event("claude.dead", sess.key, time.time(), "statusfile"))
            return
        self.bus.emit(Event("claude.file", sess.key, time.time(), "statusfile",
                            {"session": sess,
                             "recent_turn_end": self._recent_turn_end(
                                 sess.key, sess.state_since)}))

    async def _claude_sweep(self) -> None:
        while True:
            await asyncio.sleep(self.cfg.get("claude", "sweep_s"))
            for key, s in list(self.reducer.sessions.items()):
                if s.source != "claude" or s.pid is None or s.state == SessionState.ENDED:
                    continue
                if not claude_code.pid_alive(s.pid):
                    self.bus.emit(Event("claude.dead", key, time.time(), "timeout"))
            self._scan_claude()   # catch files awatch missed

    async def transcript_watcher(self) -> None:
        from watchfiles import awatch
        if not claude_code.PROJECTS_DIR.is_dir():
            return
        async for changes in awatch(claude_code.PROJECTS_DIR):
            for _, path in changes:
                if not path.endswith(".jsonl"):
                    continue
                self._on_transcript_change(path)

    def _on_transcript_change(self, path: str) -> None:
        s = self.reducer.find_by_transcript(path)
        if s is None:
            # transcript may appear after the session file did
            stem = Path(path).stem
            s = self.reducer.find_by_session_id(stem)
            if s is None:
                return
            s.transcript_path = path
        self.ensure_tail(s)
        pair = self.tails.get(s.key)
        if not pair:
            return
        tail, analyzer = pair
        changed = False
        for entries in tail.read_available():
            changed |= analyzer.feed(entries)
        if changed:
            kind = "transcript.activity" if s.source == "claude" else "codex.activity"
            payload = analyzer.activity()
            payload["live"] = True
            self.bus.emit(Event(kind, s.key, time.time(), "transcript", payload))

    async def codex_poller(self) -> None:
        while True:
            try:
                self._poll_codex_once()
                self.diagnostics.ok("codex_poller")
            except Exception as e:
                self.diagnostics.error("codex_poller", e)
            await asyncio.sleep(self.cfg.get("codex", "poll_s"))

    def _poll_codex_once(self) -> None:
        sessions, succeeded = codex.scan_with_status(self.cfg)
        seen = {s.key for s in sessions}
        for sess in sessions:
            self._codex_missing_polls.pop(sess.key, None)
            self.bus.emit(Event("codex.thread", sess.key, time.time(), "poll",
                                {"session": sess}))
        if succeeded:
            for key, existing in list(self.reducer.sessions.items()):
                if (existing.source == "codex" and not key.startswith("wsl:")
                        and key not in seen):
                    misses = self._codex_missing_polls.get(key, 0) + 1
                    self._codex_missing_polls[key] = misses
                    if existing.entrypoint == "cli" and codex.cli_terminal_alive(existing):
                        continue
                    if misses >= 2:
                        self._codex_missing_polls.pop(key, None)
                        self.bus.emit(Event("codex.missing", key, time.time(), "poll"))

    async def codex_rollout_watcher(self) -> None:
        from watchfiles import awatch
        sessions_dir = codex.CODEX_DIR / "sessions"
        if not sessions_dir.is_dir():
            return
        async for changes in awatch(sessions_dir):
            for _, path in changes:
                if not path.endswith(".jsonl"):
                    continue
                self._on_transcript_change(path)

    async def wsl_poller(self) -> None:
        """Poll normalized sessions from configured WSL distributions."""
        from .sources import wsl
        if not self.cfg.get("wsl", "enabled"):
            return
        # Asked once, before the loop, and the source is left unregistered when
        # the answer is no -- so `doctor` says nothing about WSL rather than
        # reporting a permanent failure. wsl.exe exists on every Windows install
        # whether or not WSL does, so "the binary is there" was never evidence;
        # a machine that simply does not use WSL was failing this probe every
        # five seconds and spawning a process each time to do it.
        #
        # A distribution installed later is picked up on the next daemon start.
        # That is the trade for not re-probing forever, and it is the right way
        # round: installing WSL is a deliberate act, and this is a poller for a
        # feature that is off for most users.
        if not await asyncio.to_thread(wsl.available):
            LOG.info("WSL is not installed; not polling for WSL sessions")
            return
        known = {key for key in self.reducer.sessions if key.startswith("wsl:")}
        while self.cfg.get("wsl", "enabled"):
            seen: set[str] = set()
            complete = True
            for distro in self.cfg.get("wsl", "distros") or [""]:
                sessions, succeeded = await asyncio.to_thread(wsl.scan, distro)
                complete &= succeeded
                for sess in sessions:
                    seen.add(sess.key)
                    kind = "claude.file" if sess.source == "claude" else "codex.thread"
                    self.bus.emit(Event(kind, sess.key, time.time(), "poll",
                                        {"session": sess}))
            if complete:
                for key in known - seen:
                    self.bus.emit(Event("session.hide", key, time.time(), "poll"))
                known = seen
                self.diagnostics.ok("wsl_poller")
            else:
                self.diagnostics.error("wsl_poller", RuntimeError("WSL probe failed"))
            await asyncio.sleep(self.cfg.get("wsl", "poll_s"))

    async def desktop_poller(self) -> None:
        from .sources import claude_desktop
        while self.cfg.get("claude_desktop", "enabled"):
            try:
                sess = claude_desktop.scan()
                if sess is not None:
                    self.bus.emit(Event("desktop.tile", sess.key, time.time(), "poll",
                                        {"session": sess}))
                elif "claude-desktop" in self.reducer.sessions:
                    self.bus.emit(Event("claude.dead", "claude-desktop",
                                        time.time(), "poll"))
                self.diagnostics.ok("desktop_poller")
            except Exception as e:
                self.diagnostics.error("desktop_poller", e)
            await asyncio.sleep(self.cfg.get("claude_desktop", "poll_s"))

    async def chatgpt_desktop_poller(self) -> None:
        from .sources import chatgpt_desktop
        while self.cfg.get("chatgpt_desktop", "enabled"):
            try:
                sess = chatgpt_desktop.scan()
                if sess is not None:
                    self.bus.emit(Event("desktop.tile", sess.key, time.time(), "poll",
                                        {"session": sess}))
                elif "chatgpt-desktop" in self.reducer.sessions:
                    self.bus.emit(Event("claude.dead", "chatgpt-desktop", time.time(), "poll"))
                self.diagnostics.ok("chatgpt_desktop_poller")
            except Exception as e:
                self.diagnostics.error("chatgpt_desktop_poller", e)
            await asyncio.sleep(self.cfg.get("chatgpt_desktop", "poll_s"))

    async def ticker(self) -> None:
        while True:
            await asyncio.sleep(5)
            ages = {}
            for key, (_, an) in self.tails.items():
                if isinstance(an, ClaudeAnalyzer):
                    age = an.oldest_pending_age()
                    if age is not None:
                        ages[key] = age
            self.bus.emit(Event("tick", None, time.time(), "timeout",
                                {"pending_ages": ages}))
            self._flush_snapshot_if_dirty()

    # --------------------------------------------------------- reducer loop
    async def reducer_loop(self) -> None:
        while True:
            ev = await self.bus.events.get()
            try:
                changed = self.reducer.apply(ev)
                self.diagnostics.ok("reducer")
            except Exception as e:
                self.diagnostics.error("reducer", e)
                continue
            for s in changed:
                self.ensure_tail(s)
                if s.key in self.reducer.sessions:
                    self.bus.broadcast("session.upsert", s.to_dict())
                    self.blurbs.request(s)
            for key in self.reducer.removed:
                self.tails.pop(key, None)
                self.bus.broadcast("session.remove", {"key": key})
            if changed or self.reducer.removed:
                self.mark_dirty()
                self.bus.broadcast(
                    "triage.changed",
                    build_triage(self.reducer.sessions.values()),
                )
            att = self.reducer.attention_count()
            if att != self._last_attention:
                self._last_attention = att
                self.bus.broadcast("attention.count", {"count": att})

    # --------------------------------------------------------------- server
    async def _run_plugin_source(self, plugin, source) -> None:
        context = SourceContext(
            plugin_name=plugin.name,
            source_name=source.name,
            config=self.cfg,
            bus=self.bus,
            diagnostics=self.diagnostics,
            _existing_keys=lambda: tuple(self.reducer.sessions),
        )
        try:
            await source.run(context)
            context.ok()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            context.error(exc)

    async def run(self, open_browser: bool = True) -> int:
        import uvicorn
        from .server.app import create_app

        config.ensure_state_dirs()
        self._restore_snapshot()
        # Restored sessions whose transcripts never change again (e.g. an
        # agent parked on a question) never reach _on_transcript_change, so
        # without this their analyzers stay unseeded: stale restored state is
        # never corrected and hook-time tail disambiguation is blind.
        for s in list(self.reducer.sessions.values()):
            self.ensure_tail(s)
        host = self.cfg.get("server", "host")
        port = self.cfg.get("server", "port")
        # Own the port before rotating credentials or publishing daemon.json.
        # A connect-probe is not enough: ownership files used to be written
        # before serve() bound the socket, so during a restart two daemons
        # could both pass the probe -- the loser then clobbered the winner's
        # files and deleted daemon.json on exit, leaving a healthy daemon the
        # CLI could not discover. Binding is atomic; exactly one process wins.
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        # POSIX: avoid TIME_WAIT bind failures on quick restart. Not on
        # Windows, where SO_REUSEADDR would let a second bind steal the port.
        if os.name != "nt":
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
        except OSError:
            sock.close()
            raise OSError("address already in use") from None
        self.token = config.write_token()
        self.refresh_token = config.get_or_create_refresh_token()
        app = create_app(self)
        # SSE connections are intentionally long-lived. On restart they may
        # not observe disconnect quickly enough for Uvicorn's unbounded drain,
        # leaving a live PID with no listening socket. Bound graceful shutdown
        # so the menu app can reliably launch the replacement daemon.
        uv_cfg = uvicorn.Config(
            app, host=host, port=port, log_level="warning",
            timeout_graceful_shutdown=2,
        )
        server = uvicorn.Server(uv_cfg)
        self._server = server
        # Uvicorn handles SIGTERM by setting should_exit, but on the way out of
        # capture_signals() it restores the default handler and *re-raises* the
        # captured signal. For SIGINT that re-raise becomes KeyboardInterrupt,
        # which propagates through the finally below; for SIGTERM the default
        # action terminates the process on the spot, so the teardown never runs
        # and daemon.json, the token, and the raven descriptor are all orphaned
        # (issue #43). Quit in the macOS menu-bar app sends SIGTERM, so this was
        # the ordinary stop path, not an edge case. Asking for the same orderly
        # shutdown SIGINT gets means should_exit is already set when uvicorn
        # installs its handler, and nothing is left to re-raise.
        self._install_termination_handler(server)

        tasks = [asyncio.create_task(c) for c in (
            self.reducer_loop(), self.claude_watcher(), self.transcript_watcher(),
            self.codex_poller(), self.codex_rollout_watcher(), self.ticker(),
            self.desktop_poller(),
            self.chatgpt_desktop_poller(),
            self.wsl_poller(),
        )]
        tasks.extend(
            asyncio.create_task(self._run_plugin_source(plugin, source))
            for plugin, source in self.plugins.sources()
        )
        self._write_daemon_state(port)
        # Published after the bind above, never before: a descriptor naming a
        # port that is not yet listening makes the menu bar report a healthy
        # daemon as unreachable during startup (issue #40). Best-effort -- an
        # unwritable shared directory must not stop the daemon serving.
        try:
            raven.publish(port)
            self.diagnostics.ok("raven_descriptor")
        except OSError as e:
            self.diagnostics.error("raven_descriptor", e)
        if open_browser:
            # The token rides in a URL fragment (#t=...), which browsers
            # never send over the network -- see issue #23. app.js reads it
            # once and strips it from the visible URL.
            asyncio.get_event_loop().call_later(
                0.8, webbrowser.open, f"http://{host}:{port}/#t={self.token}")
        try:
            await server.serve(sockets=[sock])
        finally:
            for t in tasks:
                t.cancel()
            # Own the blurb/chat tasks too -- their subprocess children must
            # not outlive a daemon shutdown (issue #16). asyncio.run()'s own
            # teardown would eventually cancel these anyway, but doing it
            # explicitly here means it doesn't depend on that implementation
            # detail, and it happens before the snapshot write below.
            for t in list(self.blurbs._pending.values()):
                t.cancel()
            if self.active_chat and not self.active_chat.done():
                self.active_chat.cancel()
            with contextlib.suppress(Exception):
                self._write_snapshot()   # best-effort: survive a graceful restart
            # A stopped raven should have no descriptor rather than a stale one.
            # Ownership-checked inside withdraw(), like the daemon.json teardown
            # below, so a daemon that lost the port race cannot delete the
            # winner's file (issue #40).
            with contextlib.suppress(Exception):
                raven.withdraw()
            with contextlib.suppress(Exception):
                state_path = config.STATE_DIR / "daemon.json"
                state = json.loads(state_path.read_text(encoding="utf-8"))
                if state.get("pid") == os.getpid():
                    state_path.unlink()
            with contextlib.suppress(Exception):
                if config.TOKEN_PATH.read_text(encoding="utf-8").strip() == self.token:
                    config.TOKEN_PATH.unlink()
        return 0

    def _install_termination_handler(self, server) -> None:
        """Turn a terminating signal into uvicorn's own graceful shutdown.

        Installed via the event loop rather than ``signal.signal`` so the flag is
        set from inside the loop, and installed *before* ``server.serve()`` so
        uvicorn's ``capture_signals`` records ours as the handler to restore --
        which is what stops it re-raising into a lethal default (issue #43).

        SIGHUP is included because a daemon started from a terminal that then
        closes gets it, and losing the teardown there orphans exactly the same
        files. Windows has neither signal in the asyncio loop and raises
        NotImplementedError; the tray owns lifecycle there (see WINDOWS.md), so
        a missing handler is correct rather than a gap to paper over.

        Deliberately **not** extracted to corvidae with the rest of the daemon
        machinery (issue #42). Muninn needs the same *outcome* -- a terminating
        signal that runs its cleanup -- by a mechanism with nothing in common:
        it has no async server and no uvicorn, so ``signal.signal(SIGTERM,
        lambda *_: sys.exit(0))`` is its whole implementation. The substance of
        this method is the two constraints in the paragraphs above (install via
        the loop, install *before* ``serve()``), and both exist only because
        uvicorn is in the picture. A shared helper would either lose them --
        making it a three-line wrapper around ``signal.signal`` -- or carry a
        uvicorn-shaped parameter into a stdlib-only package. Duplication is the
        smaller cost; see the "Not in scope" section of corvidae's README.
        """
        import signal

        loop = asyncio.get_running_loop()
        for name in ("SIGTERM", "SIGHUP"):
            sig = getattr(signal, name, None)
            if sig is None:
                continue
            with contextlib.suppress(NotImplementedError, RuntimeError, ValueError):
                loop.add_signal_handler(sig, self._request_shutdown, server, name)

    def _request_shutdown(self, server, signal_name: str) -> None:
        # should_exit (not force_exit) so in-flight requests still drain within
        # timeout_graceful_shutdown, matching what Ctrl-C already did.
        LOG.info("huginn: %s received, shutting down", signal_name)
        server.should_exit = True

    def request_stop(self, *, restart: bool = False) -> bool:
        """Ask the running server to shut down gracefully. True if it will.

        This is what a menu-bar **Quit**/**Restart** row reaches (see
        ``raven.perform_action``), and it deliberately sets the same
        ``should_exit`` flag a SIGTERM does rather than adding a shutdown path of
        its own. That matters because the reliable teardown is the *finally* in
        ``run()``: it withdraws the raven descriptor, ``daemon.json``, and the
        token, and it only runs if uvicorn returns from ``serve()`` normally
        (issue #43). A hard kill from here -- the thing the superseded Swift app
        escalated to -- would orphan exactly those three files.

        ``should_exit``, not ``force_exit``: the caller is an in-flight HTTP
        request, and forcing would drop the very response that tells the menu bar
        the quit was accepted. This returns *without* waiting, so the response is
        written first and the shutdown happens once the loop is next free -- which
        is the whole reason the action does not simply exit.

        Returns False when there is no server to stop (a daemon constructed in a
        test, or one not yet serving) so the caller can report that rather than
        silently appearing to succeed.
        """
        server = self._server
        if server is None:
            return False
        # Recorded before the flag is set: once should_exit is observed the loop
        # may unwind at any moment, and the exit path reads this.
        self._restart_requested = bool(restart)
        LOG.info("huginn: menu asked for %s", "restart" if restart else "quit")
        server.should_exit = True
        return True

    def _write_daemon_state(self, port: int) -> None:
        # "python"/"repo" were added so a tray app could relaunch a dead daemon
        # without guessing where Huginn lives -- the macOS app used to hardcode
        # one developer's checkout (issue #37).
        #
        # **Nothing executes them any more.** Both menu-bar apps that did are
        # deleted, and their replacement (Roost) deliberately starts nothing, so
        # these two fields now have no consumer in this repository. They are kept
        # rather than dropped because the file is a documented surface others read
        # (`huginn doctor` reports on it, and an external script may well parse
        # it), and because "which interpreter is this daemon running under" is
        # genuinely useful diagnostic output. If a future reader wants to remove
        # them, the thing to check first is that nothing outside this tree reads
        # them -- not that nothing inside it does.
        #
        # 0600, not the bare write_text's 0644 -- issue #41 M5. The original
        # reason was that the Swift app *executed* the "python" path from here, so
        # integrity mattered where confidentiality did not. That executor is gone
        # and the mode stays: it costs nothing, its 0600 siblings (token,
        # sessions.json) already set the precedent, and re-loosening a file
        # because its most dangerous reader happens to be absent today is how it
        # ends up 0644 when the next one arrives.
        state_path = config.STATE_DIR / "daemon.json"
        state_path.write_text(json.dumps({
            "pid": os.getpid(),
            "port": port,
            "started": time.time(),
            "python": sys.executable,
            "repo": str(Path(__file__).resolve().parent.parent),
        }), encoding="utf-8")
        state_path.chmod(0o600)
        (config.STATE_DIR / "port").write_text(str(port), encoding="utf-8")


def run(cfg: config.Config, open_browser: bool = True) -> int:
    """Serve until asked to stop, coming back up if a Restart was requested.

    The loop is what makes the menu bar's **Restart** row work, and it restarts
    *in this process* rather than re-execing. That is deliberate: re-exec would
    mean building an argv and an interpreter path to run, which is the
    write-then-execute shape issue #41 M5 hardened ``daemon.json`` against. There
    is nothing to resolve here -- the code is already loaded and the config is
    already parsed.

    A fresh ``Daemon`` per iteration rather than reusing one, because a restart
    has to look like a restart to everything watching: ``boot_id`` is new (which
    is how the dashboard tells "same daemon" from "it came back"), the reducer and
    tails start clean, and the snapshot written by the previous teardown is what
    carries state across. Reusing the instance would keep a stale ``_server`` and a
    sticky ``_restart_requested``, and would make the second run's teardown the
    first run's teardown all over again.
    """
    while True:
        daemon = Daemon(cfg)
        try:
            result = asyncio.run(daemon.run(open_browser=open_browser))
        except KeyboardInterrupt:
            return 0
        except OSError as e:
            if "address already in use" in str(e).lower():
                print("huginn: daemon already running (port busy)")
                return 1
            raise
        if not daemon._restart_requested:
            return result
        # The teardown in Daemon.run has already withdrawn the descriptor, the
        # token, and daemon.json, and uvicorn closed the listening socket it was
        # handed -- so the next iteration binds the same port cleanly and
        # republishes. Never open a browser on the way back: a restart is not a
        # first launch, and the user is looking at the menu, not asking for a tab.
        LOG.info("huginn: restarting")
        open_browser = False
