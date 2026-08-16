"""corvidae: shared internals for the raven agent-monitoring projects.

Created for issue #42. Huginn and Muninn both need the same things -- a robust
seek-from-end JSONL transcript tailer, credential redaction, one agreed session
shape, a start-at-login supervisor per OS (issue #39), and the shared-directory
descriptor protocol that puts them both in one status menu bar -- and duplicating
them meant either a commit-pin relationship or a second buggy reimplementation.

This package depends on nothing (stdlib only) and imports nothing from
``huginn``. Dependency direction is one-way: consumers depend on corvidae.

The names re-exported here are the stable surface. See README.md for the exact
signatures and the compatibility promise (stable within a CalVer year).
"""
from __future__ import annotations

from .descriptor import (
    STATE_DIR_ENV,
    descriptor_is_live,
    descriptor_path,
    publish_descriptor,
    read_descriptor,
    state_dir,
    withdraw_descriptor,
)
from .label import MAX_DETAIL, MAX_LABEL, sanitize_label
from .login_agent import (
    LaunchdAgent,
    LoginAgent,
    LoginAgentSpec,
    SystemdUserAgent,
    WindowsStartupAgent,
    get_login_agent,
    launch_descriptor,
)
from .model import ATTENTION_STATES, STATE_RANK, Session, SessionState
from .redact import redact_secrets
from .transcript import ATTACH_WINDOW, MAX_ATTACH_LINE, MAX_READ, ClaudeAnalyzer, CodexAnalyzer, Tail

__version__ = "2026.08.16.1"

# Anything not listed here is an implementation detail carrying no promise --
# issue #42 asked for a declared surface, so the absence of a name from this
# list is itself the answer to "can I depend on it?" (no).
__all__ = [
    "ATTACH_WINDOW",
    "ATTENTION_STATES",
    "MAX_ATTACH_LINE",
    "MAX_DETAIL",
    "MAX_LABEL",
    "MAX_READ",
    "STATE_DIR_ENV",
    "STATE_RANK",
    "ClaudeAnalyzer",
    "CodexAnalyzer",
    "LaunchdAgent",
    "LoginAgent",
    "LoginAgentSpec",
    "Session",
    "SessionState",
    "SystemdUserAgent",
    "Tail",
    "WindowsStartupAgent",
    "descriptor_is_live",
    "descriptor_path",
    "get_login_agent",
    "launch_descriptor",
    "publish_descriptor",
    "read_descriptor",
    "redact_secrets",
    "sanitize_label",
    "state_dir",
    "withdraw_descriptor",
]
