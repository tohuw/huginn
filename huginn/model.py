"""Core data shapes: Session, SessionState, Event."""
from __future__ import annotations

import dataclasses
import enum
from typing import Any


class SessionState(str, enum.Enum):
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
    tokens: int | None = None
    version: str | None = None

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


@dataclasses.dataclass
class Event:
    kind: str                      # file.status | hook.* | transcript.activity | codex.thread | proc.dead | ...
    session_key: str | None
    ts: float
    origin: str                    # hook | statusfile | transcript | poll | timeout
    payload: dict[str, Any] = dataclasses.field(default_factory=dict)
