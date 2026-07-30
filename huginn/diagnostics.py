"""Bounded, rate-limited record of background-task failures -- issue #15.

Several long-running loops (codex_poller, desktop_poller, reducer_loop,
blurb generation, snapshot/notification writes) used to swallow every
exception silently: a source could go dark and the dashboard would just
look stale, with nothing to point at why. Every one of those now reports
into a Diagnostics registry instead of a bare `except: pass`.

Redaction: str(exception) can carry transcript/prompt/token content
unpredictably (a KeyError repr'ing a dict key, a path embedding a prompt
excerpt, ...). The registry only ever stores the exception *class name*
plus counts/timestamps -- never the message text. Full tracebacks go to
the stderr logger only (captured by the LaunchAgent log for local
debugging), never to anything exposed over the API.
"""
from __future__ import annotations

import logging
import time
import traceback
from dataclasses import dataclass, field

LOG = logging.getLogger("huginn.diagnostics")
LOG_INTERVAL_S = 30.0
MAX_LOG_INTERVAL_S = 3600.0


@dataclass
class SourceHealth:
    last_success: float | None = None
    last_error_class: str | None = None
    last_error_ts: float | None = None
    error_count: int = 0
    _last_logged: float = field(default=0.0, repr=False, compare=False)
    _log_interval: float = field(default=LOG_INTERVAL_S, repr=False, compare=False)

    def to_dict(self) -> dict:
        return {
            "last_success": self.last_success,
            "last_error_class": self.last_error_class,
            "last_error_ts": self.last_error_ts,
            "error_count": self.error_count,
        }


class Diagnostics:
    def __init__(self) -> None:
        self.sources: dict[str, SourceHealth] = {}

    def ok(self, source: str) -> None:
        h = self.sources.setdefault(source, SourceHealth())
        now = time.time()
        if h.last_error_ts and (h.last_success is None or h.last_success < h.last_error_ts):
            h._last_logged = 0.0
            h._log_interval = LOG_INTERVAL_S
        h.last_success = now

    def error(self, source: str, exc: BaseException) -> None:
        now = time.time()
        h = self.sources.setdefault(source, SourceHealth())
        h.last_error_class = type(exc).__name__
        h.last_error_ts = now
        h.error_count += 1
        if now - h._last_logged >= h._log_interval:
            h._last_logged = now
            tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
            LOG.error("%s failed (%d total so far): %s", source, h.error_count, tb)
            h._log_interval = min(h._log_interval * 2, MAX_LOG_INTERVAL_S)

    def snapshot(self) -> dict:
        """Redacted: class name + counts + timestamps only, no message text."""
        return {name: h.to_dict() for name, h in self.sources.items()}
