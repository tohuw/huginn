"""Backward-compatible re-export of the transcript tailer, now in ``corvidae``.

The implementation moved to ``corvidae.transcript`` -- issue #42: it is shared
verbatim with Muninn, which needed a declared stable surface rather than a
commit pin. This module stays because plugins and downstream forks import
``huginn.sources.transcript`` directly, and breaking them is not acceptable.

The private helpers are re-exported too (``_items``/``_user_text`` are used by
``huginn.llm.context``): they carry no stability promise in corvidae, but
huginn's own modules are inside the version boundary, so importing them is fine.
"""
from __future__ import annotations

from corvidae.transcript import (  # noqa: F401  -- re-export for import compatibility
    ATTACH_WINDOW,
    MAX_ATTACH_LINE,
    MAX_READ,
    ClaudeAnalyzer,
    CodexAnalyzer,
    Tail,
    _items,
    _iso_ts,
    _parse_lines,
    _strip_meta,
    _user_text,
)

__all__ = [
    "ATTACH_WINDOW",
    "MAX_ATTACH_LINE",
    "MAX_READ",
    "ClaudeAnalyzer",
    "CodexAnalyzer",
    "Tail",
]
