"""Blurb worker: one-line LLM summaries generated on state changes only.

Debounced per session, globally rate-capped, silent on failure. The global
llm.enabled toggle short-circuits before any subprocess is spawned.
"""
from __future__ import annotations

import asyncio
import time
from collections import deque
from typing import TYPE_CHECKING

from ..model import Session, SessionState
from .context import distill
from .providers import compatible_model, get_provider

if TYPE_CHECKING:
    from ..daemon import Daemon

BLURB_STATES = {
    SessionState.WAITING_INPUT, SessionState.WAITING_PERMISSION,
    SessionState.ERROR, SessionState.DONE, SessionState.WORKING, SessionState.IDLE,
}

PROMPT = """You monitor AI coding-agent sessions. Summarize in ONE line, max 12 words,
present tense: what this session is doing / what it needs from the user.
Output only that line - no preamble, no quotes.

Session: {name}  state: {state}  cwd: {cwd}

Recent activity:
{tail}
"""

TITLE_PROMPT = """Give this AI coding session a short title, maximum 5 words.
Describe its current task, not its state. Output only the title, no quotes.

Session: {name}  cwd: {cwd}
Recent activity:
{tail}
"""


class BlurbWorker:
    def __init__(self, daemon: "Daemon"):
        self.daemon = daemon
        self._pending: dict[str, asyncio.Task] = {}
        self._recent: deque[float] = deque(maxlen=60)

    def request(self, s: Session) -> None:
        cfg = self.daemon.cfg
        old = self._pending.pop(s.key, None)
        if old and not old.done():
            old.cancel()
        if not cfg.get("llm", "enabled") or (s.title and s.state not in BLURB_STATES):
            return
        if s.title and s.blurb_ts and s.blurb_ts > s.state_since:
            return  # already blurbed this state; metadata churn shouldn't re-spend
        self._pending[s.key] = asyncio.create_task(self._debounced(s.key, s.state))

    def set_enabled(self, enabled: bool) -> None:
        if not enabled:
            for task in self._pending.values():
                if not task.done():
                    task.cancel()
            self._pending.clear()
            return
        for session in self.daemon.reducer.sessions.values():
            self.request(session)

    async def _debounced(self, key: str, state: SessionState) -> None:
        cfg = self.daemon.cfg
        await asyncio.sleep(cfg.get("llm", "blurb_debounce_s"))
        if not cfg.get("llm", "enabled"):
            return
        s = self.daemon.reducer.sessions.get(key)
        if s is None or s.state != state:
            return  # state moved on while we waited; a newer request owns it
        tail = "\n".join(distill(s.transcript_path or "", s.source, max_lines=25))
        if not s.title:
            guessed = await self._run_prompt(
                TITLE_PROMPT.format(name=s.name, cwd=s.cwd, tail=tail),
                reserve=1 if s.state in BLURB_STATES else 0)
            current = self.daemon.reducer.sessions.get(key)
            if guessed and current is not None and not current.title:
                current.title = guessed[:60]
                current.title_origin = "guessed"
                self.daemon.mark_dirty()
                self.daemon.bus.broadcast("session.upsert", current.to_dict())
        if s.state not in BLURB_STATES:
            return
        text = await self._run_prompt(
            PROMPT.format(name=s.name, state=s.state.value, cwd=s.cwd, tail=tail))
        if not text:
            return
        first_line = text.splitlines()[0].strip()
        s = self.daemon.reducer.sessions.get(key)
        if s is None or s.state != state:
            return
        s.blurb = first_line[:120]
        s.blurb_ts = time.time()
        self.daemon.mark_dirty()
        self.daemon.bus.broadcast("session.upsert", s.to_dict())

    async def _run_prompt(self, prompt: str, reserve: int = 0) -> str | None:
        cfg = self.daemon.cfg
        now = time.time()
        cap = cfg.get("llm", "blurb_max_per_min")
        if len([t for t in self._recent if now - t < 60]) >= cap - reserve:
            return None
        self._recent.append(now)
        try:
            from .. import config as _config
            _config.ensure_state_dirs()
            provider_name = cfg.get("llm", "provider")
            text = await get_provider(provider_name, self.daemon.plugins).run_text(
                prompt, model=compatible_model(
                    provider_name, cfg.get("llm", "blurb_model"), self.daemon.plugins),
                timeout=cfg.get("llm", "blurb_timeout_s"),
                cwd=str(_config.CACHE_DIR))
            self.daemon.diagnostics.ok("blurb")
        except Exception as e:
            # the card keeps its old blurb -- but the failure is now
            # recorded (issue #15) instead of vanishing silently.
            self.daemon.diagnostics.error("blurb", e)
            return None
        return text.splitlines()[0].strip() if text else None
