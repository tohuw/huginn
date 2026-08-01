"""Budgeted automatic titles and one-line summaries for live sessions.

Requests are coalesced per session, cached by exact evidence, bounded per
minute and per UTC day, and protected by a provider-wide failure circuit.
The global ``llm.enabled`` toggle short-circuits before any subprocess or
remote provider call is made.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from .. import config as _config
from .. import policy
from ..model import Session, SessionState
from ..plugins import LLMProviderError
from .context import distill, evidence_text
from .providers import blurb_model, effective_provider_name, get_provider

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

BACKOFF_INITIAL_S = 60.0
BACKOFF_MAX_S = 3600.0
BUDGET_FILENAME = "llm-budget.json"
RESPONSE_CACHE_MAX = 1000


class BlurbWorker:
    def __init__(self, daemon: "Daemon"):
        self.daemon = daemon
        self._pending: dict[str, asyncio.Task] = {}
        self._rerun: set[str] = set()
        self._provider_lock = asyncio.Lock()
        self._recent: deque[float] = deque(maxlen=60)
        self._responses: dict[str, str] = {}
        self._circuit_signature: tuple[str, str] | None = None
        self._circuit_failures = 0
        self._circuit_retry_at = 0.0
        self._circuit_permanent = False
        self._budget_path: Path | None = None
        self._budget_day = ""
        self._budget_calls = 0

    def request(self, s: Session) -> None:
        cfg = self.daemon.cfg
        if not cfg.get("llm", "enabled") or (s.title and s.state not in BLURB_STATES):
            return
        old = self._pending.get(s.key)
        if old and not old.done():
            self._rerun.add(s.key)
            return
        if s.title and s.blurb_ts and s.blurb_ts > s.state_since:
            return  # already blurbed this state; metadata churn shouldn't re-spend
        task = asyncio.create_task(self._debounced(s.key, s.state))
        self._pending[s.key] = task
        task.add_done_callback(lambda done, key=s.key: self._task_done(key, done))

    def _task_done(self, key: str, task: asyncio.Task) -> None:
        if self._pending.get(key) is not task:
            return
        self._pending.pop(key, None)
        if key not in self._rerun:
            return
        self._rerun.discard(key)
        current = self.daemon.reducer.sessions.get(key)
        if current is not None:
            self.request(current)

    def set_enabled(self, enabled: bool) -> None:
        self._reset_circuit()
        if not enabled:
            for task in self._pending.values():
                if not task.done():
                    task.cancel()
            self._pending.clear()
            self._rerun.clear()
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
                TITLE_PROMPT.format(
                    name=evidence_text(s.name, 180),
                    cwd=evidence_text(s.cwd, 500),
                    tail=tail,
                ),
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
            PROMPT.format(
                name=evidence_text(s.name, 180),
                state=s.state.value,
                cwd=evidence_text(s.cwd, 500),
                tail=tail,
            ))
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

    def _reset_circuit(self) -> None:
        self._circuit_signature = None
        self._circuit_failures = 0
        self._circuit_retry_at = 0.0
        self._circuit_permanent = False

    def _sync_circuit(self, provider: str, configured_model: str) -> None:
        signature = (provider, configured_model)
        if signature != self._circuit_signature:
            self._reset_circuit()
            self._circuit_signature = signature

    def _open_circuit(self, exc: BaseException, now: float) -> None:
        self._circuit_failures += 1
        if getattr(exc, "retryable", True) is False:
            self._circuit_permanent = True
            self._circuit_retry_at = 0.0
            return
        delay = min(
            BACKOFF_INITIAL_S * (2 ** (self._circuit_failures - 1)),
            BACKOFF_MAX_S,
        )
        self._circuit_retry_at = now + delay

    def _circuit_open(self, now: float) -> bool:
        return self._circuit_permanent or now < self._circuit_retry_at

    @staticmethod
    def _utc_day(now: float) -> str:
        return datetime.fromtimestamp(now, timezone.utc).date().isoformat()

    def _load_budget(self, now: float) -> None:
        path = _config.CACHE_DIR / BUDGET_FILENAME
        if self._budget_path == path:
            if self._budget_day != self._utc_day(now):
                self._budget_day = self._utc_day(now)
                self._budget_calls = 0
            return
        self._budget_path = path
        self._budget_day = self._utc_day(now)
        self._budget_calls = 0
        try:
            data = json.loads(path.read_text())
            if data.get("day") == self._budget_day:
                self._budget_calls = max(0, int(data.get("calls", 0)))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            pass

    def _write_budget(self) -> None:
        assert self._budget_path is not None
        _config.ensure_state_dirs()
        tmp = self._budget_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({
            "day": self._budget_day,
            "calls": self._budget_calls,
        }))
        tmp.chmod(0o600)
        os.replace(tmp, self._budget_path)

    def _claim_daily_budget(self, now: float, reserve: int) -> bool:
        self._load_budget(now)
        cap = self.daemon.cfg.get("llm", "blurb_max_per_day")
        if self._budget_calls >= cap - reserve:
            return False
        self._budget_calls += 1
        try:
            self._write_budget()
            self.daemon.diagnostics.ok("blurb_budget")
        except OSError as exc:
            # Keep enforcing the in-memory count even if persistence fails.
            self.daemon.diagnostics.error("blurb_budget", exc)
        return True

    def status(self) -> dict:
        now = time.time()
        self._load_budget(now)
        limit = self.daemon.cfg.get("llm", "blurb_max_per_day")
        return {
            "enabled": bool(self.daemon.cfg.get("llm", "enabled")),
            "pending": len(self._pending),
            "cached_responses": len(self._responses),
            "budget": {
                "day": self._budget_day,
                "used": self._budget_calls,
                "limit": limit,
                "remaining": max(0, limit - self._budget_calls),
            },
            "circuit": {
                "open": self._circuit_open(now),
                "permanent": self._circuit_permanent,
                "failures": self._circuit_failures,
                "retry_at": self._circuit_retry_at or None,
            },
        }

    async def _run_prompt(self, prompt: str, reserve: int = 0) -> str | None:
        # Serializing automatic calls makes the first provider failure visible
        # to every queued session before another request can escape.
        async with self._provider_lock:
            return await self._run_prompt_serialized(prompt, reserve)

    async def _run_prompt_serialized(self, prompt: str, reserve: int = 0) -> str | None:
        cfg = self.daemon.cfg
        now = time.time()
        provider_name = cfg.get("llm", "provider")
        configured_model = cfg.get("llm", "blurb_model")
        self._sync_circuit(provider_name, configured_model)
        if self._circuit_open(now):
            return None
        cap = cfg.get("llm", "blurb_max_per_min")
        if len([t for t in self._recent if now - t < 60]) >= cap - reserve:
            return None
        try:
            provider = get_provider(provider_name, self.daemon.plugins)
            if provider is None:
                # issue #41 C2: no silent fallback to ClaudeCLI for an unknown
                # name. retryable=False so the circuit latches -- a provider
                # that is not installed will not become installed by waiting.
                raise LLMProviderError(
                    f"no installed provider named {provider_name!r}", retryable=False)
            unavailable = provider.available()
            if unavailable:
                raise LLMProviderError(
                    f"{provider_name} is unavailable", retryable=False)
            # Gate on the resolved provider's own name, so the policy verdict
            # describes the object that will actually run (issue #41 C2).
            provider_name = effective_provider_name(provider, provider_name)
            model = blurb_model(
                provider_name, configured_model, self.daemon.plugins)
            # issue #41: automatic text is the highest-volume core LLM call, so
            # it routes through the chokepoint too. PolicyRefused carries
            # retryable=False, so _open_circuit latches permanently instead of
            # re-attempting a refused model every minute for every session.
            policy.check(model, provider_name)
            fingerprint = hashlib.sha256(
                f"{provider_name}\0{model}\0{prompt}".encode()
            ).hexdigest()
            if fingerprint in self._responses:
                return self._responses[fingerprint]
            if not self._claim_daily_budget(now, reserve):
                return None
            self._recent.append(now)
            _config.ensure_state_dirs()
            text = await provider.run_text(
                prompt, model=model,
                timeout=cfg.get("llm", "blurb_timeout_s"),
                cwd=str(_config.CACHE_DIR))
            first_line = text.splitlines()[0].strip() if text else ""
            if not first_line:
                raise LLMProviderError("provider returned no text")
            if len(self._responses) >= RESPONSE_CACHE_MAX:
                self._responses.pop(next(iter(self._responses)))
            self._responses[fingerprint] = first_line
            self._circuit_failures = 0
            self._circuit_retry_at = 0.0
            self.daemon.diagnostics.ok("blurb")
        except Exception as e:
            self._open_circuit(e, now)
            self.daemon.diagnostics.error("blurb", e)
            return None
        return first_line
