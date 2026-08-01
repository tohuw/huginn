"""Core data shapes: Session, SessionState, Event.

``Session``, ``SessionState``, ``STATE_RANK`` and ``ATTENTION_STATES`` moved to
``corvidae.model`` -- issue #42: plugins already imported them from here, so they
needed a declared stable surface instead of an implicit commit pin. They are
re-exported below, unchanged, so ``from huginn.model import Session`` keeps
working exactly as before for every existing plugin and fork.

``Event`` stayed: it is this daemon's internal bus envelope, not a shared shape,
and the extraction was deliberately kept narrow.
"""
from __future__ import annotations

import dataclasses
from typing import Any

from corvidae.model import (  # noqa: F401  -- re-export for import compatibility
    ATTENTION_STATES,
    STATE_RANK,
    Session,
    SessionState,
)


@dataclasses.dataclass
class Event:
    kind: str                      # file.status | hook.* | transcript.activity | codex.thread | proc.dead | ...
    session_key: str | None
    ts: float
    origin: str                    # hook | statusfile | transcript | poll | timeout
    payload: dict[str, Any] = dataclasses.field(default_factory=dict)


__all__ = ["ATTENTION_STATES", "STATE_RANK", "Event", "Session", "SessionState"]
