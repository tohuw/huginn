"""The daemon: wires sources → bus → reducer → SSE server."""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import socket
import time
import webbrowser
from pathlib import Path

from . import config
from .bus import Bus
from .diagnostics import Diagnostics
from .model import Event, Session, SessionState
from .plugins import SourceContext, get_registry
from .sources import claude_code, codex
from .sources.transcript import ClaudeAnalyzer, CodexAnalyzer, Tail
from .state import Reducer
from .triage import build_triage


class Daemon:
    def __init__(self, cfg: config.Config):
        self.cfg = cfg
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
        self.diagnostics = Diagnostics()   # issue #15
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
            data = json.loads(self.SNAPSHOT_PATH.read_text())
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
        tmp.write_text(data)
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
            raw = json.loads(path.read_text())
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
        host = self.cfg.get("server", "host")
        port = self.cfg.get("server", "port")
        # Do this before rotating credentials or publishing daemon.json. A
        # losing second launch used to clobber the healthy daemon's ownership
        # files and then enter a restart loop after Uvicorn reported EADDRINUSE.
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.settimeout(0.2)
            if probe.connect_ex((host, port)) == 0:
                raise OSError("address already in use")
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
        if open_browser:
            # The token rides in a URL fragment (#t=...), which browsers
            # never send over the network -- see issue #23. app.js reads it
            # once and strips it from the visible URL.
            asyncio.get_event_loop().call_later(
                0.8, webbrowser.open, f"http://{host}:{port}/#t={self.token}")
        try:
            await server.serve()
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
            with contextlib.suppress(Exception):
                state_path = config.STATE_DIR / "daemon.json"
                state = json.loads(state_path.read_text())
                if state.get("pid") == os.getpid():
                    state_path.unlink()
            with contextlib.suppress(Exception):
                if config.TOKEN_PATH.read_text().strip() == self.token:
                    config.TOKEN_PATH.unlink()
        return 0

    def _write_daemon_state(self, port: int) -> None:
        (config.STATE_DIR / "daemon.json").write_text(json.dumps(
            {"pid": os.getpid(), "port": port, "started": time.time()}))
        (config.STATE_DIR / "port").write_text(str(port))


def run(cfg: config.Config, open_browser: bool = True) -> int:
    daemon = Daemon(cfg)
    try:
        return asyncio.run(daemon.run(open_browser=open_browser))
    except KeyboardInterrupt:
        return 0
    except OSError as e:
        if "address already in use" in str(e).lower():
            print("huginn: daemon already running (port busy)")
            return 1
        raise
