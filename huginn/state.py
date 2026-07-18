"""The reducer: session registry + transition rules.

Evidence priority: hook > transcript > statusfile > poll/timeout. A state set
by a hook holds for a grace window against lower-priority contradictions
(hooks are edge-triggered and precise; the status file lags).
"""
from __future__ import annotations

import time
from pathlib import Path

from .config import Config
from .model import ATTENTION_STATES, Event, Session, SessionState

_ORIGIN_PRIORITY = {"hook": 3, "transcript": 2, "statusfile": 1, "poll": 1, "timeout": 0, "init": 0}
HOOK_GRACE_S = 3.0


class Reducer:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.sessions: dict[str, Session] = {}
        self.removed: list[str] = []          # keys removed on last apply

    # ------------------------------------------------------------------ util
    def _set_state(self, s: Session, state: SessionState, origin: str, now: float) -> bool:
        """Apply a state respecting origin priority + hook grace. True if changed."""
        if s.state == state:
            return False
        cur_pri = _ORIGIN_PRIORITY.get(s.state_origin, 0)
        new_pri = _ORIGIN_PRIORITY.get(origin, 0)
        if (s.state_origin == "hook" and now - s.state_since < HOOK_GRACE_S
                and new_pri < _ORIGIN_PRIORITY["hook"]):
            return False
        # WORKING from the status file is strong evidence regardless of what
        # lower-latency sources said earlier: busy means the process is running.
        if new_pri < cur_pri and not (state == SessionState.WORKING and origin == "statusfile"):
            # allow lower-priority evidence only once the current state has aged
            if now - s.state_since < 30:
                return False
        s.state = state
        s.state_origin = origin
        s.state_since = now
        return True

    def find_by_session_id(self, session_id: str) -> Session | None:
        for s in self.sessions.values():
            if s.session_id == session_id:
                return s
        return None

    def find_by_transcript(self, path: str) -> Session | None:
        for s in self.sessions.values():
            if s.transcript_path == path:
                return s
        return None

    def attention_count(self) -> int:
        return sum(1 for s in self.sessions.values() if s.state in ATTENTION_STATES)

    # ----------------------------------------------------------------- apply
    def apply(self, ev: Event) -> list[Session]:
        """Returns sessions whose visible state changed."""
        self.removed = []
        now = ev.ts or time.time()
        handler = getattr(self, "_on_" + ev.kind.replace(".", "_"), None)
        if handler is None:
            return []
        return handler(ev, now)

    # claude status file appeared/changed
    def _on_claude_file(self, ev: Event, now: float) -> list[Session]:
        incoming: Session = ev.payload["session"]
        s = self.sessions.get(incoming.key)
        if s is None:
            self.sessions[incoming.key] = incoming
            return [incoming]
        changed = False
        for attr in ("name", "version", "entrypoint", "cwd"):
            v = getattr(incoming, attr)
            if v and v != getattr(s, attr):
                setattr(s, attr, v); changed = True
        if incoming.transcript_path and not s.transcript_path:
            s.transcript_path = incoming.transcript_path; changed = True
        s.last_activity = max(s.last_activity, incoming.last_activity)
        if incoming.state == SessionState.WORKING:
            changed |= self._set_state(s, SessionState.WORKING, "statusfile", now)
        elif incoming.state == SessionState.IDLE and s.state == SessionState.WORKING:
            # busy->idle flip: if a turn just ended in the transcript, that's DONE
            recent_end = ev.payload.get("recent_turn_end", False)
            target = SessionState.DONE if recent_end else SessionState.IDLE
            changed |= self._set_state(s, target, "statusfile", now)
        return [s] if changed else []

    def _on_claude_dead(self, ev: Event, now: float) -> list[Session]:
        s = self.sessions.get(ev.session_key or "")
        if s is None:
            return []
        died_mid_work = s.source == "claude" and s.state == SessionState.WORKING
        s.state = SessionState.ERROR if died_mid_work else SessionState.ENDED
        s.state_origin = "timeout"
        s.state_since = now
        return [s]

    def _on_transcript_activity(self, ev: Event, now: float) -> list[Session]:
        s = self.sessions.get(ev.session_key or "")
        if s is None:
            return []
        a = ev.payload
        changed = False
        for attr, key in (("git_branch", "git_branch"), ("model", "model"),
                          ("tokens", "tokens"), ("last_prompt", "last_prompt")):
            v = a.get(key)
            if v and v != getattr(s, attr):
                setattr(s, attr, v); changed = True
        s.last_activity = now
        if a.get("error"):
            changed |= self._set_state(s, SessionState.ERROR, "transcript", now)
        elif a.get("asked_user_question"):
            changed |= self._set_state(s, SessionState.WAITING_INPUT, "transcript", now)
        elif a.get("live"):   # fresh lines flowing = it's working
            changed |= self._set_state(s, SessionState.WORKING, "transcript", now)
        return [s] if changed else []

    def _on_codex_thread(self, ev: Event, now: float) -> list[Session]:
        incoming: Session = ev.payload["session"]
        s = self.sessions.get(incoming.key)
        if s is None:
            self.sessions[incoming.key] = incoming
            return [incoming]
        changed = False
        for attr in ("name", "model", "git_branch", "tokens", "cwd", "transcript_path", "last_prompt"):
            v = getattr(incoming, attr)
            if v and v != getattr(s, attr):
                setattr(s, attr, v); changed = True
        s.last_activity = max(s.last_activity, incoming.last_activity)
        # poll-derived state only ever refreshes low-confidence states
        if s.state_origin in ("poll", "init", "timeout"):
            changed |= self._set_state(s, incoming.state, "poll", now)
        return [s] if changed else []

    def _on_codex_activity(self, ev: Event, now: float) -> list[Session]:
        s = self.sessions.get(ev.session_key or "")
        if s is None:
            return []
        a = ev.payload
        changed = False
        for attr in ("model", "tokens", "last_prompt"):
            v = a.get(attr)
            if v and v != getattr(s, attr):
                setattr(s, attr, v); changed = True
        s.last_activity = now
        phase_state = {
            "working": SessionState.WORKING,
            "done": SessionState.DONE,
            "error": SessionState.ERROR,
            "aborted": SessionState.IDLE,
        }.get(a.get("phase") or "")
        if phase_state:
            changed |= self._set_state(s, phase_state, "transcript", now)
        return [s] if changed else []

    # Claude Desktop tile updates share the codex upsert semantics
    def _on_desktop_tile(self, ev: Event, now: float) -> list[Session]:
        return self._on_codex_thread(ev, now)

    # hook events (installed in M3; reducer rules live here from the start)
    def _on_hook_claude(self, ev: Event, now: float) -> list[Session]:
        data = ev.payload.get("data", {})
        event = ev.payload.get("event", "")
        session_id = data.get("session_id", "")
        s = self.find_by_session_id(session_id)
        if s is None:
            return []
        if not s.transcript_path and data.get("transcript_path"):
            s.transcript_path = data["transcript_path"]
        changed = False
        if event == "UserPromptSubmit":
            prompt = (data.get("prompt") or "")[:300]
            if prompt:
                s.last_prompt = prompt; changed = True
            changed |= self._set_state(s, SessionState.WORKING, "hook", now)
        elif event == "Notification":
            msg = (data.get("message") or "").lower()
            pats = self.cfg.section("patterns")
            if any(p in msg for p in pats["permission"]):
                target = SessionState.WAITING_PERMISSION
            else:
                target = SessionState.WAITING_INPUT
            changed |= self._set_state(s, target, "hook", now)
        elif event == "Stop":
            target = SessionState.WAITING_INPUT if ev.payload.get("asked_question") \
                else SessionState.DONE
            changed |= self._set_state(s, target, "hook", now)
        elif event == "SessionEnd":
            changed |= self._set_state(s, SessionState.ENDED, "hook", now)
        elif event == "SessionStart":
            s.last_activity = now
        return [s] if changed else []

    def _on_tick(self, ev: Event, now: float) -> list[Session]:
        changed: list[Session] = []
        pending_timeout = self.cfg.get("claude", "pending_tool_timeout_s")
        ended_ttl = self.cfg.get("ui", "ended_ttl_s")
        for key, s in list(self.sessions.items()):
            # a tool_use stuck without result while not busy = permission prompt
            age = ev.payload.get("pending_ages", {}).get(key)
            if (age is not None and age > pending_timeout
                    and s.state in (SessionState.IDLE, SessionState.WORKING)):
                if self._set_state(s, SessionState.WAITING_PERMISSION, "transcript", now):
                    changed.append(s)
            if s.state == SessionState.ENDED and now - s.state_since > ended_ttl:
                del self.sessions[key]
                self.removed.append(key)
        return changed
