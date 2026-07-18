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
from .providers import get_provider

if TYPE_CHECKING:
    from ..daemon import Daemon

# Only decision points get a blurb; WORKING/IDLE churn constantly and would burn quota.
BLURB_STATES = {
    SessionState.WAITING_INPUT, SessionState.WAITING_PERMISSION,
    SessionState.ERROR, SessionState.DONE,
}

PROMPT = """You monitor AI coding-agent sessions. Summarize in ONE line, max 12 words,
present tense: what this session is doing / what it needs from the user.
Output only that line - no preamble, no quotes.

Session: {name}  state: {state}  cwd: {cwd}

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
        if not cfg.get("llm", "enabled") or s.state not in BLURB_STATES:
            return
        if s.blurb_ts and s.blurb_ts > s.state_since:
            return  # already blurbed this state; metadata churn shouldn't re-spend
        old = self._pending.pop(s.key, None)
        if old and not old.done():
            old.cancel()
        self._pending[s.key] = asyncio.create_task(self._debounced(s.key, s.state))

    async def _debounced(self, key: str, state: SessionState) -> None:
        cfg = self.daemon.cfg
        await asyncio.sleep(cfg.get("llm", "blurb_debounce_s"))
        s = self.daemon.reducer.sessions.get(key)
        if s is None or s.state != state:
            return  # state moved on while we waited; a newer request owns it
        now = time.time()
        cap = cfg.get("llm", "blurb_max_per_min")
        if len([t for t in self._recent if now - t < 60]) >= cap:
            return
        self._recent.append(now)
        tail = "\n".join(distill(s.transcript_path or "", s.source, max_lines=25))
        prompt = PROMPT.format(name=s.name, state=s.state.value, cwd=s.cwd, tail=tail)
        try:
            from .. import config as _config
            _config.ensure_state_dirs()
            text = await get_provider(cfg.get("llm", "provider")).run_text(
                prompt, model=cfg.get("llm", "blurb_model"),
                timeout=cfg.get("llm", "blurb_timeout_s"),
                cwd=str(_config.CACHE_DIR))
        except Exception:
            return  # silent: the card keeps its old blurb
        first_line = text.splitlines()[0].strip() if text else ""
        if not first_line:
            return
        s = self.daemon.reducer.sessions.get(key)
        if s is None:
            return
        s.blurb = first_line[:120]
        s.blurb_ts = time.time()
        self.daemon.bus.broadcast("session.upsert", s.to_dict())
