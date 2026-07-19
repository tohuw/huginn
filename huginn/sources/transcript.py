"""Seek-from-end JSONL tailer + per-dialect analyzers (Claude transcripts, Codex rollouts).

Transcripts are unbounded (170MB corpus on this machine) — never read from the
front. Attach at size-64KB, then incremental reads past a stored offset.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Iterator

ATTACH_WINDOW = 64 * 1024
MAX_READ = 256 * 1024
MAX_ATTACH_LINE = 4 * 1024 * 1024

_TASK_NOTIF_TOOL_ID_RE = re.compile(r"<tool-use-id>(.*?)</tool-use-id>")
_TASK_NOTIF_STATUS_RE = re.compile(r"<status>(.*?)</status>")


class Tail:
    def __init__(self, path: str):
        self.path = Path(path)
        self.offset = 0
        self._partial = b""

    def attach(self) -> list[dict]:
        """Seed from the last ATTACH_WINDOW bytes; returns parsed entries."""
        try:
            size = self.path.stat().st_size
        except OSError:
            return []
        start = max(0, size - ATTACH_WINDOW)
        with self.path.open("rb") as f:
            # A tool result or embedded payload can make one JSONL record much
            # larger than the normal attach window. Widen backwards, bounded,
            # until the record boundary is found instead of silently dropping
            # every entry in the window.
            while start > 0:
                earlier = max(0, start - ATTACH_WINDOW)
                f.seek(earlier)
                prefix = f.read(start - earlier)
                boundary = prefix.rfind(b"\n")
                if boundary >= 0:
                    start = earlier + boundary + 1
                    break
                start = earlier
                if size - start >= MAX_ATTACH_LINE:
                    break
            f.seek(start)
            data = f.read(size - start)
        if start > 0:
            nl = data.find(b"\n")
            data = data[nl + 1:] if nl >= 0 else b""
        self.offset = size
        self._partial = b""
        return _parse_lines(data, keep_partial=self)

    def read_new(self) -> list[dict]:
        try:
            size = self.path.stat().st_size
        except OSError:
            return []
        if size < self.offset:          # truncated/rotated under us
            return self.attach()
        if size == self.offset:
            return []
        with self.path.open("rb") as f:
            f.seek(self.offset)
            data = f.read(min(size - self.offset, MAX_READ))
        self.offset += len(data)
        data = self._partial + data
        self._partial = b""
        return _parse_lines(data, keep_partial=self)

    def read_available(self) -> Iterator[list[dict]]:
        """Yield parsed chunks until all bytes currently on disk are consumed.

        A watcher may coalesce a multi-megabyte append into one notification;
        MAX_READ limits each I/O operation, not the total handled for that
        notification.
        """
        while True:
            before = self.offset
            entries = self.read_new()
            if entries:
                yield entries
            if self.offset == before:
                return
            try:
                if self.offset >= self.path.stat().st_size:
                    return
            except OSError:
                return


def _parse_lines(data: bytes, keep_partial: Tail | None = None) -> list[dict]:
    entries: list[dict] = []
    if not data:
        return entries
    lines = data.split(b"\n")
    if lines and lines[-1]:             # trailing partial line — keep for next read
        if keep_partial is not None:
            keep_partial._partial = lines[-1]
        lines = lines[:-1]
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
            if isinstance(obj, dict):
                entries.append(obj)
        except json.JSONDecodeError:
            continue
    return entries


# ---------------------------------------------------------------- Claude dialect

class ClaudeAnalyzer:
    """Folds Claude transcript entries into rolling activity state."""

    def __init__(self) -> None:
        self.pending_tools: dict[str, float] = {}   # tool_use id -> ts seen
        self.last_prompt: str | None = None
        self.last_assistant_text: str | None = None
        self.last_entry_type: str | None = None
        self.git_branch: str | None = None
        self.model: str | None = None
        self.tokens: int | None = None
        self.error: bool = False
        self.asked_user_question: bool = False
        self.last_ts: float = 0.0
        self.subagents: dict[str, str] = {}   # Agent tool_use id -> running/completed/failed/killed

    def feed(self, entries: list[dict]) -> bool:
        """Returns True if anything meaningful changed."""
        changed = False
        for e in entries:
            etype = e.get("type")
            if etype == "queue-operation":
                # Subagent completion is reported via a synthetic entry (not
                # user/assistant/system) whose content carries a
                # <task-notification> block -- see issue #8 research notes.
                if self._feed_task_notification(e.get("content") or ""):
                    changed = True
                continue
            if etype not in ("user", "assistant", "system"):
                continue
            changed = True
            self.last_entry_type = etype
            self.git_branch = e.get("gitBranch") or self.git_branch
            ts = e.get("timestamp")
            if isinstance(ts, str):
                self.last_ts = _iso_ts(ts) or self.last_ts
            msg = e.get("message") or {}
            content = msg.get("content")
            if etype == "system":
                if e.get("isApiErrorMessage") or e.get("level") == "error":
                    self.error = True
                continue
            self.error = False
            if etype == "assistant":
                self.model = msg.get("model") or self.model
                usage = msg.get("usage") or {}
                total = sum(v for k, v in usage.items()
                            if isinstance(v, int) and k.endswith("_tokens"))
                if total:
                    self.tokens = total
                self.asked_user_question = False
                for item in _items(content):
                    it = item.get("type")
                    if it == "tool_use":
                        self.pending_tools[item.get("id", "?")] = time.time()
                        if item.get("name") == "AskUserQuestion":
                            self.asked_user_question = True
                        elif item.get("name") == "Agent":
                            self.subagents[item.get("id", "?")] = "running"
                    elif it == "text" and item.get("text"):
                        self.last_assistant_text = item["text"][-2000:]
            elif etype == "user":
                got_result = False
                for item in _items(content):
                    if item.get("type") == "tool_result":
                        self.pending_tools.pop(item.get("tool_use_id", ""), None)
                        got_result = True
                if not got_result and not e.get("isSidechain"):
                    text = _user_text(content)
                    if text:
                        self.last_prompt = text[:300]
                        self.pending_tools.clear()   # new turn resets stale pendings
                        self.asked_user_question = False
        return changed

    def oldest_pending_age(self) -> float | None:
        if not self.pending_tools:
            return None
        return time.time() - min(self.pending_tools.values())

    def _feed_task_notification(self, content: str) -> bool:
        tool_use_id = _TASK_NOTIF_TOOL_ID_RE.search(content)
        status = _TASK_NOTIF_STATUS_RE.search(content)
        if not tool_use_id or not status:
            return False
        self.subagents[tool_use_id.group(1)] = status.group(1)
        return True

    def _subagent_counts(self) -> dict[str, int] | None:
        if not self.subagents:
            return None
        counts: dict[str, int] = {}
        for status in self.subagents.values():
            label = "done" if status == "completed" else status
            counts[label] = counts.get(label, 0) + 1
        return counts

    def activity(self) -> dict[str, Any]:
        return {
            "pending_tools": len(self.pending_tools),
            "oldest_pending_age": self.oldest_pending_age(),
            "last_entry_type": self.last_entry_type,
            "last_prompt": self.last_prompt,
            "last_assistant_text": self.last_assistant_text,
            "asked_user_question": self.asked_user_question,
            "git_branch": self.git_branch,
            "model": self.model,
            "tokens": self.tokens,
            "error": self.error,
            "last_ts": self.last_ts,
            "subagents": self._subagent_counts(),
        }


# ----------------------------------------------------------------- Codex dialect

class CodexAnalyzer:
    """Folds Codex rollout entries into rolling activity state."""

    def __init__(self) -> None:
        self.phase: str | None = None       # working | done | error | aborted
        self.last_prompt: str | None = None
        self.last_agent_text: str | None = None
        self.model: str | None = None
        self.tokens: int | None = None
        self.last_ts: float = 0.0

    def feed(self, entries: list[dict]) -> bool:
        changed = False
        for e in entries:
            payload = e.get("payload") or {}
            ts = e.get("timestamp")
            if isinstance(ts, str):
                self.last_ts = _iso_ts(ts) or self.last_ts
            if e.get("type") == "turn_context":
                self.model = payload.get("model") or self.model
                continue
            if e.get("type") != "event_msg":
                continue
            ptype = payload.get("type")
            if ptype == "task_started":
                self.phase = "working"; changed = True
            elif ptype == "task_complete":
                self.phase = "done"; changed = True
            elif ptype in ("turn_aborted", "task_aborted"):
                self.phase = "aborted"; changed = True
            elif ptype in ("error", "stream_error", "task_failed"):
                self.phase = "error"; changed = True
            # Codex exposes native command/file approval and user-input request
            # types. Keep the aliases defensive across CLI/app protocol names;
            # the pending-tool timeout remains a fallback for older rollouts.
            elif ptype in (
                "exec_approval_request", "apply_patch_approval_request",
                "command_execution_approval_request", "file_change_approval_request",
                "permissions_approval_request",
            ):
                self.phase = "waiting_permission"; changed = True
            elif ptype in ("request_user_input", "elicitation_request"):
                self.phase = "waiting_input"; changed = True
            elif ptype == "user_message":
                self.last_prompt = str(payload.get("message", ""))[:300]
                self.phase = "working"; changed = True
            elif ptype == "agent_message":
                self.last_agent_text = str(payload.get("message", ""))[-2000:]
                changed = True
            elif ptype == "token_count":
                info = payload.get("info") or payload
                total = info.get("total_token_usage") or {}
                self.tokens = total.get("total_tokens") or self.tokens
        return changed

    def activity(self) -> dict[str, Any]:
        return {
            "phase": self.phase,
            "last_prompt": self.last_prompt,
            "last_assistant_text": self.last_agent_text,
            "model": self.model,
            "tokens": self.tokens,
            "last_ts": self.last_ts,
        }


# --------------------------------------------------------------------- helpers

def _items(content: Any) -> list[dict]:
    if isinstance(content, list):
        return [c for c in content if isinstance(c, dict)]
    return []


_META_TAG_RE = None


def _strip_meta(text: str) -> str:
    """Drop harness-injected XML-ish blocks (<ide_opened_file>, <system-reminder>,
    <command-name>...) so last_prompt shows what the human actually typed."""
    global _META_TAG_RE
    import re
    if _META_TAG_RE is None:
        _META_TAG_RE = re.compile(
            r"<(ide_opened_file|ide_selection|system-reminder|command-name|"
            r"command-message|command-args|local-command-stdout|task-notification)>"
            r".*?</\1>", re.DOTALL)
    return _META_TAG_RE.sub("", text).strip()


def _user_text(content: Any) -> str | None:
    text = None
    if isinstance(content, str):
        text = content
    else:
        for item in _items(content):
            if item.get("type") == "text" and item.get("text"):
                text = item["text"]
                break
    if not text:
        return None
    return _strip_meta(text) or None


def _iso_ts(s: str) -> float | None:
    import datetime
    try:
        return datetime.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None
