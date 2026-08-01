"""corvidae: shared internals for the raven agent-monitoring projects.

Created for issue #42. Huginn and Muninn both need the same three things --
a robust seek-from-end JSONL transcript tailer, credential redaction for
transcript text, and one agreed session shape -- and duplicating them meant
either a commit-pin relationship or a second buggy reimplementation.

This package depends on nothing (stdlib only) and imports nothing from
``huginn``. Dependency direction is one-way: consumers depend on corvidae.

The names re-exported here are the stable surface. See README.md for the exact
signatures and the compatibility promise (stable within a CalVer year).
"""
from __future__ import annotations

from .model import ATTENTION_STATES, STATE_RANK, Session, SessionState
from .redact import redact_secrets
from .transcript import ATTACH_WINDOW, MAX_ATTACH_LINE, MAX_READ, ClaudeAnalyzer, CodexAnalyzer, Tail

__version__ = "2026.07.31"

# Anything not listed here is an implementation detail carrying no promise --
# issue #42 asked for a declared surface, so the absence of a name from this
# list is itself the answer to "can I depend on it?" (no).
__all__ = [
    "ATTACH_WINDOW",
    "ATTENTION_STATES",
    "MAX_ATTACH_LINE",
    "MAX_READ",
    "STATE_RANK",
    "ClaudeAnalyzer",
    "CodexAnalyzer",
    "Session",
    "SessionState",
    "Tail",
    "redact_secrets",
]
