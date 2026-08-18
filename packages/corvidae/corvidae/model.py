"""Agent-session data shapes shared by the raven projects -- issue #42.

``Session``/``SessionState``/``STATE_RANK``/``ATTENTION_STATES`` are part of the
stable surface documented in this package's README. They were extracted from
``huginn.model`` unchanged, so a consumer that already imported them from Huginn
gets identical behaviour with an actual compatibility promise attached.

Huginn's daemon-internal ``Event`` deliberately did NOT come along: it is bus
plumbing for one daemon, not a shared shape, and the point of the extraction was
a *tight* surface.
"""
from __future__ import annotations

import dataclasses
import enum
from typing import Any


class SessionState(str, enum.Enum):
    ACTIVE = "active"             # app tile: renderer activity, not agent work
    WORKING = "working"
    WAITING_INPUT = "waiting_input"
    WAITING_PERMISSION = "waiting_permission"
    DONE = "done"
    ERROR = "error"
    IDLE = "idle"
    ENDED = "ended"


ATTENTION_STATES = {
    SessionState.WAITING_INPUT,
    SessionState.WAITING_PERMISSION,
    SessionState.ERROR,
}

# Dashboard sort: lower rank = higher urgency.
STATE_RANK = {
    SessionState.WAITING_PERMISSION: 0,
    SessionState.WAITING_INPUT: 1,
    SessionState.ERROR: 2,
    SessionState.DONE: 3,
    SessionState.WORKING: 4,
    SessionState.IDLE: 5,
    SessionState.ENDED: 6,
    SessionState.ACTIVE: 7,        # app tiles are grouped separately in the UI
}


@dataclasses.dataclass
class Session:
    key: str                       # "claude:<pid>" | "codex:<thread_id>" | "claude-desktop"
    source: str                    # claude | codex | claude-desktop
    session_id: str
    cwd: str
    name: str
    pid: int | None = None
    git_branch: str | None = None
    model: str | None = None
    entrypoint: str | None = None  # cli | claude-vscode | vscode | ...
    state: SessionState = SessionState.IDLE
    state_since: float = 0.0       # epoch seconds
    state_origin: str = "init"     # hook | statusfile | transcript | timeout | init
    transcript_path: str | None = None
    tty: str | None = None
    blurb: str | None = None
    blurb_ts: float | None = None
    last_activity: float = 0.0     # epoch seconds
    last_prompt: str | None = None
    # Bounded current evidence supplied by an installed non-transcript source.
    # Kept distinct from blurbs: this is authoritative source data, not an LLM
    # interpretation, and may therefore be included in Ask digests.
    source_summary: str | None = None
    tokens: int | None = None
    version: str | None = None
    subagents: dict[str, int] | None = None   # e.g. {"running": 2, "done": 1}
    shells: int = 0                 # live shell subprocesses owned by the agent
    title: str | None = None        # ephemeral card label; dies with this session
    title_origin: str | None = None # manual | guessed
    # Optional dashboard section for this session, separate from the main
    # grid -- the same treatment built-in desktop-app tiles get (grouped,
    # sorted outside the urgency queue, one collective show/hide toggle),
    # generalized so a plugin source can opt into it. group is a short stable
    # key; group_label is the human-readable section heading (falls back to
    # group itself if unset). Unset means "render in the main grid," exactly
    # like every session before this field existed.
    group: str | None = None
    group_label: str | None = None
    # Display text for the per-card source badge (falls back to `source`
    # itself if unset, same convention as group/group_label). Lets a plugin's
    # internal source name -- lowercase, hyphenated, matched against
    # SourceContext.upsert()'s validation regex -- differ from what a user
    # sees on the card, e.g. source="neo-cortex" but source_label="NeoCortex".
    source_label: str | None = None
    # Optional installed-plugin focus route.  This is deliberately just a
    # registered handler name, never a URL or other external target: roster
    # and Ask payloads are safe to expose without leaking a private deep link.
    focus_handler: str | None = None
    # How to reach this session's *tab*, when its terminal can say.
    #
    # A pid identifies a process, not a place on screen, and on Windows that
    # gap is total: Windows Terminal runs every window and tab in one process
    # behind one HWND, so every session on the machine resolves to the same
    # window and "jump" lands on whichever tab was already showing. Nothing in
    # its UI Automation tree distinguishes them either.
    #
    # Terminals that *can* say do it through the environment of the process
    # they host -- WezTerm exports WEZTERM_PANE, its control socket and its own
    # executable path into every pane. Anything running in that pane can read
    # them, which is how this gets filled in: a hook fires inside the session
    # and reports what its terminal told it. Shape is ``{"kind": ..., ...}``,
    # left open because each terminal names its own coordinates.
    terminal: dict[str, str] | None = None

    @property
    def attention(self) -> bool:
        return self.state in ATTENTION_STATES

    def to_dict(self) -> dict[str, Any]:
        d = dataclasses.asdict(self)
        d["state"] = self.state.value
        d["rank"] = STATE_RANK[self.state]
        d["attention"] = self.attention
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Session":
        """Inverse of to_dict(), for restoring a persisted snapshot."""
        fields = {f.name for f in dataclasses.fields(cls)}
        kwargs = {k: v for k, v in d.items() if k in fields}
        kwargs["state"] = SessionState(kwargs["state"])
        return cls(**kwargs)


__all__ = ["ATTENTION_STATES", "STATE_RANK", "Session", "SessionState"]
