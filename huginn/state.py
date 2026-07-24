"""The reducer: session registry + transition rules.

Evidence priority: hook > transcript > statusfile > poll/timeout. A state set
by a hook holds for a grace window against lower-priority contradictions
(hooks are edge-triggered and precise; the status file lags).
"""
from __future__ import annotations

import time

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
        # A blurb summarizes the previous state. Carrying it across a state
        # transition turns a once-correct blocker into false current context.
        s.blurb = None
        s.blurb_ts = None
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

    # ------------------------------------------------------------- persistence
    def snapshot(self) -> dict:
        """Non-ENDED sessions, for surviving a daemon restart. Callers re-scan
        live sources right after restore(); origin-priority rules in
        _set_state naturally correct anything stale within a few seconds."""
        return {k: s.to_dict() for k, s in self.sessions.items()
                if s.state != SessionState.ENDED}

    def restore(self, data: dict) -> None:
        for key, d in data.items():
            try:
                self.sessions[key] = Session.from_dict(d)
            except (KeyError, ValueError, TypeError):
                continue

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
        if s.blurb and (not s.blurb_ts or s.blurb_ts < s.state_since):
            s.blurb = None; s.blurb_ts = None; changed = True
        for attr in ("name", "version", "entrypoint", "cwd"):
            v = getattr(incoming, attr)
            if v and v != getattr(s, attr):
                setattr(s, attr, v); changed = True
        if incoming.shells != s.shells:
            s.shells = incoming.shells; changed = True
        if incoming.transcript_path and not s.transcript_path:
            s.transcript_path = incoming.transcript_path; changed = True
        s.last_activity = max(s.last_activity, incoming.last_activity)
        if incoming.state == SessionState.WORKING:
            # Claude leaves its coarse status at `shell` while background
            # Bash tasks survive a completed turn. A later, clean assistant
            # turn-end is stronger evidence about agent activity; the shells
            # remain visible separately on the card.
            if ev.payload.get("recent_turn_end") and incoming.shells:
                changed |= self._set_state(s, SessionState.DONE, "transcript", now)
            else:
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
                          ("tokens", "tokens"), ("last_prompt", "last_prompt"),
                          ("subagents", "subagents")):
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
        if s.blurb and (not s.blurb_ts or s.blurb_ts < s.state_since):
            s.blurb = None; s.blurb_ts = None; changed = True
        for attr in ("name", "model", "git_branch", "tokens", "cwd", "transcript_path",
                    "last_prompt", "source_summary", "subagents", "group", "group_label"):
            v = getattr(incoming, attr)
            if (v or attr == "source_summary") and v != getattr(s, attr):
                setattr(s, attr, v); changed = True
        s.last_activity = max(s.last_activity, incoming.last_activity)
        # Poll evidence is allowed to correct stale hook/transcript states;
        # _set_state preserves the 3s hook grace and 30s origin-priority
        # window. The old origin gate made a missed task_complete capable of
        # pinning a Codex card in WORKING forever.
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
            "waiting_permission": SessionState.WAITING_PERMISSION,
            "waiting_input": SessionState.WAITING_INPUT,
        }.get(a.get("phase") or "")
        if phase_state:
            changed |= self._set_state(s, phase_state, "transcript", now)
        return [s] if changed else []

    def _on_codex_missing(self, ev: Event, now: float) -> list[Session]:
        """A complete live-roster scan no longer includes this thread."""
        s = self.sessions.get(ev.session_key or "")
        if s is None:
            return []
        del self.sessions[s.key]
        self.removed.append(s.key)
        return []

    def _on_session_hide(self, ev: Event, now: float) -> list[Session]:
        """Remove a source record that still exists but aged out of the roster."""
        s = self.sessions.pop(ev.session_key or "", None)
        if s is not None:
            self.removed.append(s.key)
        return []

    # Claude Desktop tile updates share the codex upsert semantics
    def _on_desktop_tile(self, ev: Event, now: float) -> list[Session]:
        return self._on_codex_thread(ev, now)

    def _on_plugin_session(self, ev: Event, now: float) -> list[Session]:
        """Upsert a session emitted through the public plugin source context."""
        return self._on_codex_thread(ev, now)

    def _on_plugin_remove(self, ev: Event, now: float) -> list[Session]:
        s = self.sessions.pop(ev.session_key or "", None)
        if s is not None:
            self.removed.append(s.key)
        return []

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
            # Claude Code 2.x supplies a stable notification_type.  Keep the
            # configurable message match as a compatibility fallback for old
            # payloads and third-party hook forwarders.
            notification_type = data.get("notification_type")
            if notification_type == "permission_prompt":
                target = SessionState.WAITING_PERMISSION
            elif notification_type == "idle_prompt":
                # Claude emits this well after an ordinary completed turn. It
                # means the terminal is passively idle, not that the agent
                # asked the user a question.
                target = SessionState.DONE
            elif notification_type == "elicitation_dialog":
                target = SessionState.WAITING_INPUT
            elif notification_type in {
                "auth_success", "elicitation_complete", "elicitation_response",
            }:
                return []
            elif any(p in msg for p in pats["permission"]):
                target = SessionState.WAITING_PERMISSION
            else:
                target = SessionState.WAITING_INPUT
            # AskUserQuestion arrives as a permission-shaped notification, but
            # it's the agent asking the user a question, not a tool approval.
            # The hook endpoint disambiguates from the transcript tail (same
            # mechanism as Stop's asked_question).
            if (target is SessionState.WAITING_PERMISSION
                    and ev.payload.get("asked_question")):
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

    def _on_hook_codex(self, ev: Event, now: float) -> list[Session]:
        """Codex only fires SessionStart/UserPromptSubmit/Stop (issue #20 --
        Notification/SessionEnd aren't in its hook-event enum, so installing
        them was pure overhead; install.py stopped registering them).

        This is a lower-latency nudge layered on top of the poll/rollout
        source, not a replacement for it: origin priority (hook=3 outranks
        poll=1/transcript=2) and the existing hook grace window already
        handle a hook and a poll/rollout update disagreeing, exactly as they
        do for Claude. If the thread hasn't been discovered by a poll yet,
        this is a safe no-op -- discovery stays poll's job, same as Claude's
        hook handler does for unknown sessions.
        """
        data = ev.payload.get("data", {})
        event = ev.payload.get("event", "")
        s = self.find_by_session_id(data.get("session_id", ""))
        if s is None:
            return []
        changed = False
        if event == "UserPromptSubmit":
            changed = self._set_state(s, SessionState.WORKING, "hook", now)
        elif event == "Stop":
            # No transcript-tail disambiguation available at hook-fire-time
            # the way Claude's Stop handler gets one -- DONE is the safe
            # default; a wrong guess self-corrects via the next poll/rollout
            # update, same as any other hook/poll disagreement.
            changed = self._set_state(s, SessionState.DONE, "hook", now)
        elif event == "SessionStart":
            s.last_activity = now
        return [s] if changed else []

    def _on_tick(self, ev: Event, now: float) -> list[Session]:
        changed: list[Session] = []
        pending_timeout = self.cfg.get("claude", "pending_tool_timeout_s")
        # show_ended=False means don't show them at all, not just "for a
        # while" -- reuse the same TTL-removal path with a 0 grace period
        # rather than a separate client-side filter (issue #19).
        ended_ttl = self.cfg.get("ui", "ended_ttl_s") if self.cfg.get("ui", "show_ended") else 0
        for key, s in list(self.sessions.items()):
            # a tool_use stuck without result while not busy = permission prompt
            age = ev.payload.get("pending_ages", {}).get(key)
            if (age is not None and age > pending_timeout
                    and s.state in (SessionState.IDLE, SessionState.WORKING)):
                if self._set_state(s, SessionState.WAITING_PERMISSION, "transcript", now):
                    changed.append(s)
            if s.state == SessionState.ENDED and now - s.state_since >= ended_ttl:
                del self.sessions[key]
                self.removed.append(key)
        return changed
