"""Transcript distillation: tail -> compact human/LLM-readable digest.

Shared by Peek (dashboard), blurbs, and the Q&A chat. Never reads whole
transcripts — last 64KB only.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..sources.transcript import Tail, _items, _user_text

TRUNC = 300


def _clip(text: str, n: int = TRUNC) -> str:
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 1] + "…"


def _tool_summary(item: dict) -> str:
    name = item.get("name", "?")
    inp = item.get("input") or {}
    for key in ("command", "file_path", "path", "query", "pattern", "url", "prompt"):
        if key in inp and isinstance(inp[key], str):
            return f"{name}({_clip(inp[key], 80)})"
    return f"{name}()"


def distill_claude(entries: list[dict]) -> list[str]:
    lines: list[str] = []
    for e in entries:
        etype = e.get("type")
        msg = e.get("message") or {}
        content = msg.get("content")
        if etype == "user":
            results = [i for i in _items(content) if i.get("type") == "tool_result"]
            if results:
                for r in results:
                    lines.append("  ✗ tool error" if r.get("is_error") else "  ✓")
            elif not e.get("isSidechain"):
                text = _user_text(content)
                if text:
                    lines.append(f"user: {_clip(text)}")
        elif etype == "assistant":
            for item in _items(content):
                it = item.get("type")
                if it == "text" and item.get("text"):
                    lines.append(f"assistant: {_clip(item['text'])}")
                elif it == "tool_use":
                    lines.append(f"  → {_tool_summary(item)}")
        elif etype == "system" and (e.get("isApiErrorMessage") or e.get("level") == "error"):
            lines.append(f"ERROR: {_clip(str(e.get('content') or e.get('message') or ''))}")
    return lines


def distill_codex(entries: list[dict]) -> list[str]:
    lines: list[str] = []
    for e in entries:
        if e.get("type") != "event_msg":
            continue
        p = e.get("payload") or {}
        ptype = p.get("type")
        if ptype == "user_message":
            lines.append(f"user: {_clip(str(p.get('message', '')))}")
        elif ptype == "agent_message":
            lines.append(f"assistant: {_clip(str(p.get('message', '')))}")
        elif ptype == "task_started":
            lines.append("  ⟳ turn started")
        elif ptype == "task_complete":
            lines.append("  ■ turn complete")
        elif ptype in ("error", "stream_error", "task_failed"):
            lines.append(f"ERROR: {_clip(json.dumps(p)[:200])}")
    return lines


def distill(transcript_path: str, source: str, max_lines: int = 40) -> list[str]:
    if not transcript_path or not Path(transcript_path).exists():
        return []
    tail = Tail(transcript_path)
    entries = tail.attach()
    lines = distill_claude(entries) if source == "claude" else distill_codex(entries)
    return lines[-max_lines:]


def evidence_for_session(s: Any, max_lines: int = 40) -> list[str]:
    """Bounded authoritative evidence for a built-in or plugin session."""
    transcript = distill(s.transcript_path or "", s.source, max_lines)
    if transcript:
        return transcript
    summary = getattr(s, "source_summary", None)
    if not isinstance(summary, str):
        return []
    return [_clip(line) for line in summary.splitlines() if line.strip()][-max_lines:]


def digest_for_session(s: Any, max_lines: int = 40) -> str:
    """Markdown digest of one session — header + distilled tail."""
    head = [
        f"# {s.name} ({s.source})",
        f"- state: {s.state.value} (since {int(__import__('time').time() - s.state_since)}s ago)",
        f"- cwd: {s.cwd}",
        f"- branch: {s.git_branch or '?'}  model: {s.model or '?'}",
    ]
    # Blurbs are cached UI summaries generated at an earlier decision point.
    # They are deliberately excluded from Ask evidence: current state and the
    # transcript tail are authoritative, and a stale blurb can invent a blocker.
    body = evidence_for_session(s, max_lines)
    return "\n".join(head) + "\n\n```\n" + "\n".join(body) + "\n```\n"
