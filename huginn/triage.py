"""Deterministic roster triage and real-worktree contention detection."""
from __future__ import annotations

import time
from collections import Counter
from functools import lru_cache
from pathlib import Path
from typing import Callable, Iterable

from .model import ATTENTION_STATES, Session, SessionState


ACTIVE_WORK_STATES = frozenset({
    SessionState.WORKING,
    SessionState.WAITING_INPUT,
    SessionState.WAITING_PERMISSION,
})
LOCAL_AGENT_SOURCES = frozenset({"claude", "codex"})
ATTENTION_REASONS = {
    SessionState.WAITING_PERMISSION: "waiting for permission",
    SessionState.WAITING_INPUT: "waiting for input",
    SessionState.ERROR: "reported an error",
}


@lru_cache(maxsize=512)
def worktree_root(cwd: str) -> str | None:
    """Resolve a local cwd to its nearest Git worktree root.

    If no ``.git`` marker is present, the canonical cwd itself is returned so
    two agents in exactly the same non-Git directory can still be identified.
    Relative, empty, and NUL-bearing paths are rejected instead of being
    interpreted relative to the daemon process.
    """
    if not isinstance(cwd, str) or not cwd or "\x00" in cwd:
        return None
    path = Path(cwd).expanduser()
    if not path.is_absolute():
        return None
    try:
        canonical = path.resolve(strict=False)
    except OSError:
        return None
    for candidate in (canonical, *canonical.parents):
        try:
            if (candidate / ".git").exists():
                return str(candidate)
        except OSError:
            continue
    return str(canonical)


def _session_view(session: Session, now: float) -> dict:
    return {
        "key": session.key,
        "name": session.name,
        "source": session.source,
        "state": session.state.value,
        "cwd": session.cwd,
        "git_branch": session.git_branch,
        "age_seconds": max(0, int(now - session.state_since)),
    }


def build_triage(
    sessions: Iterable[Session],
    *,
    now: float | None = None,
    resolve_worktree: Callable[[str], str | None] = worktree_root,
) -> dict:
    """Project the live roster into stable status and contention signals."""
    observed_at = time.time() if now is None else now
    roster = list(sessions)
    counts = Counter(session.state.value for session in roster)
    attention = [
        {
            **_session_view(session, observed_at),
            "reason": ATTENTION_REASONS[session.state],
        }
        for session in roster
        if session.state in ATTENTION_STATES
    ]
    active = [
        _session_view(session, observed_at)
        for session in roster
        if session.state == SessionState.WORKING
    ]

    by_worktree: dict[str, list[Session]] = {}
    for session in roster:
        if session.source not in LOCAL_AGENT_SOURCES:
            continue
        if session.state not in ACTIVE_WORK_STATES or not session.cwd:
            continue
        root = resolve_worktree(session.cwd)
        if root:
            by_worktree.setdefault(root, []).append(session)

    contentions = []
    for root, group in sorted(by_worktree.items()):
        unique = {session.key: session for session in group}
        if len(unique) < 2:
            continue
        ordered = sorted(unique.values(), key=lambda session: session.name.lower())
        contentions.append({
            "worktree": root,
            "count": len(ordered),
            "sessions": [_session_view(session, observed_at) for session in ordered],
        })

    attention.sort(key=lambda item: (item["age_seconds"], item["name"].lower()))
    active.sort(key=lambda item: (item["age_seconds"], item["name"].lower()))
    if contentions:
        count = len(contentions)
        headline = f"{count} worktree{'s have' if count != 1 else ' has'} competing sessions"
        level = "contention"
    elif attention:
        count = len(attention)
        headline = f"{count} session{'s need' if count != 1 else ' needs'} you"
        level = "attention"
    elif active:
        count = len(active)
        headline = f"{count} session{'s are' if count != 1 else ' is'} working"
        level = "active"
    else:
        headline = "Nothing needs you right now"
        level = "clear"

    return {
        "verdict": {"level": level, "headline": headline},
        "counts": dict(sorted(counts.items())),
        "attention": attention,
        "active": active,
        "contentions": contentions,
    }


__all__ = ["ACTIVE_WORK_STATES", "build_triage", "worktree_root"]
