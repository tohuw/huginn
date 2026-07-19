"""The daemon: wires sources → bus → reducer → SSE server."""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import time
import webbrowser
from pathlib import Path

from . import config
from .bus import Bus
from .model import Event, Session, SessionState
from .sources import claude_code, codex
from .sources.transcript import ClaudeAnalyzer, CodexAnalyzer, Tail
from .state import Reducer


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
        self.hook_hits: dict[str, int] = {}   # "{source}.{event}" -> count, issue #2
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
        self.hook_hits = data.get("hook_hits", {})

    def _write_snapshot(self) -> None:
        data = json.dumps({"sessions": self.reducer.snapshot(), "hook_hits": self.hook_hits})
        tmp = self.SNAPSHOT_PATH.with_suffix(".json.tmp")
        tmp.write_text(data)
        os.replace(tmp, self.SNAPSHOT_PATH)

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

    def _recent_turn_end(self, key: str) -> bool:
        pair = self.tails.get(key)
        if not pair:
            return False
        _, an = pair
        if not isinstance(an, ClaudeAnalyzer):
            return False
        return (an.last_entry_type == "assistant" and not an.pending_tools
                and time.time() - an.last_ts < 20)

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
                            self.bus.emit(Event("claude.dead", f"claude:{p.stem}",
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
        if not (claude_code.pid_alive(sess.pid)
                and claude_code.pid_matches_start(sess.pid, raw.get("procStart"))):
            if sess.key in self.reducer.sessions:
                self.bus.emit(Event("claude.dead", sess.key, time.time(), "statusfile"))
            return
        self.bus.emit(Event("claude.file", sess.key, time.time(), "statusfile",
                            {"session": sess,
                             "recent_turn_end": self._recent_turn_end(sess.key)}))

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
        if analyzer.feed(tail.read_new()):
            kind = "transcript.activity" if s.source == "claude" else "codex.activity"
            payload = analyzer.activity()
            payload["live"] = True
            self.bus.emit(Event(kind, s.key, time.time(), "transcript", payload))

    async def codex_poller(self) -> None:
        while True:
            try:
                for sess in codex.scan(self.cfg):
                    self.bus.emit(Event("codex.thread", sess.key, time.time(), "poll",
                                        {"session": sess}))
            except Exception:
                pass
            await asyncio.sleep(self.cfg.get("codex", "poll_s"))

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
            except Exception:
                pass
            await asyncio.sleep(self.cfg.get("claude_desktop", "poll_s"))

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
            if self._dirty:
                try:
                    self._write_snapshot()
                except OSError:
                    pass
                self._dirty = False

    # --------------------------------------------------------- reducer loop
    async def reducer_loop(self) -> None:
        while True:
            ev = await self.bus.events.get()
            try:
                changed = self.reducer.apply(ev)
            except Exception:
                continue
            for s in changed:
                self.ensure_tail(s)
                self.bus.broadcast("session.upsert", s.to_dict())
                self.blurbs.request(s)
            for key in self.reducer.removed:
                self.tails.pop(key, None)
                self.bus.broadcast("session.remove", {"key": key})
            if changed or self.reducer.removed:
                self.mark_dirty()
            att = self.reducer.attention_count()
            if att != self._last_attention:
                self._last_attention = att
                self.bus.broadcast("attention.count", {"count": att})

    # --------------------------------------------------------------- server
    async def run(self, open_browser: bool = True) -> int:
        import uvicorn
        from .server.app import create_app

        config.ensure_state_dirs()
        self._restore_snapshot()
        self.token = config.write_token()
        host = self.cfg.get("server", "host")
        port = self.cfg.get("server", "port")
        app = create_app(self)
        uv_cfg = uvicorn.Config(app, host=host, port=port, log_level="warning")
        server = uvicorn.Server(uv_cfg)

        tasks = [asyncio.create_task(c) for c in (
            self.reducer_loop(), self.claude_watcher(), self.transcript_watcher(),
            self.codex_poller(), self.codex_rollout_watcher(), self.ticker(),
            self.desktop_poller(),
        )]
        self._write_daemon_state(port)
        if open_browser:
            asyncio.get_event_loop().call_later(
                0.8, webbrowser.open, f"http://{host}:{port}/")
        try:
            await server.serve()
        finally:
            for t in tasks:
                t.cancel()
            with contextlib.suppress(Exception):
                self._write_snapshot()   # best-effort: survive a graceful restart
            with contextlib.suppress(Exception):
                (config.STATE_DIR / "daemon.json").unlink()
            with contextlib.suppress(Exception):
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
