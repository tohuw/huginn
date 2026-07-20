"""Explicit, bounded, two-stage control of an existing terminal session."""
from __future__ import annotations

import json
import os
import secrets
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import config
from .focus import find_tty
from .model import Session
from .platform import platform as _platform


AUTHORITY_LEVELS = frozenset({"observe", "steer"})
AUTHORITY_PATH = config.STATE_DIR / "authorities.json"
MAX_AUTHORITY_RECORDS = 200
MAX_INSTRUCTION_CHARS = 800
MAX_CONFIRMATIONS = 50
CONFIRMATION_TTL_S = 60


def _authority_path(path: Path | None = None) -> Path:
    return path or AUTHORITY_PATH


def _load_authorities(path: Path | None = None) -> dict[str, dict]:
    target = _authority_path(path)
    try:
        value = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(value, dict):
        return {}
    return {
        key: record
        for key, record in value.items()
        if isinstance(key, str) and isinstance(record, dict)
    }


def _save_authorities(records: dict[str, dict], path: Path | None = None) -> None:
    target = _authority_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.parent.chmod(0o700)
    newest = sorted(
        records.items(),
        key=lambda item: float(item[1].get("updated_at") or 0),
        reverse=True,
    )[:MAX_AUTHORITY_RECORDS]
    fd, tmp_name = tempfile.mkstemp(prefix="authorities.", dir=target.parent)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(dict(newest), stream, separators=(",", ":"))
        tmp.chmod(0o600)
        os.replace(tmp, target)
    finally:
        tmp.unlink(missing_ok=True)


def authority_for(session: Session, path: Path | None = None) -> str:
    record = _load_authorities(path).get(session.key, {})
    if record.get("session_id") != session.session_id:
        return "observe"
    return "steer" if record.get("level") == "steer" else "observe"


def set_authority(session: Session, level: str, path: Path | None = None) -> dict:
    if level not in AUTHORITY_LEVELS:
        raise ValueError("authority must be observe or steer")
    records = _load_authorities(path)
    if level == "observe":
        records.pop(session.key, None)
    else:
        _terminal_target(session)
        records[session.key] = {
            "session_id": session.session_id,
            "level": "steer",
            "updated_at": time.time(),
        }
    _save_authorities(records, path)
    return {"key": session.key, "session_id": session.session_id, "level": level}


def validate_instruction(instruction: object) -> str:
    if not isinstance(instruction, str) or not instruction.strip():
        raise ValueError("instruction is required")
    if len(instruction) > MAX_INSTRUCTION_CHARS:
        raise ValueError(f"instruction is limited to {MAX_INSTRUCTION_CHARS} characters")
    if "\n" in instruction or "\r" in instruction:
        raise ValueError("only one line can be sent per confirmation")
    if any(ord(character) < 32 or ord(character) == 127 for character in instruction):
        raise ValueError("instruction cannot contain control characters")
    return instruction


def _require_steer(session: Session) -> None:
    if authority_for(session) != "steer":
        raise PermissionError("session is observe-only; grant steer authority first")


def _terminal_target(session: Session) -> tuple[int | None, str]:
    if session.source == "codex":
        if session.entrypoint not in {"cli", "exec"}:
            raise ValueError("only terminal Codex sessions can be steered")
        if not session.cwd:
            raise ValueError("session is not mapped to an exact terminal tab")
        matches = [
            (pid, _platform.process_tty(pid))
            for pid in _platform.find_processes("codex")
            if _platform.process_cwd(pid) == session.cwd
        ]
        targets = {(pid, tty) for pid, tty in matches if tty}
        ttys = {tty for _, tty in targets}
        if len(ttys) > 1:
            raise ValueError("multiple Codex terminal tabs match this workspace")
        pid = next((pid for pid, tty in targets if tty), None)
        tty = next(iter(ttys), None)
        if not tty:
            raise ValueError("session is not mapped to an exact terminal tab")
        return pid, tty
    elif session.source == "claude":
        if session.entrypoint != "cli" or not session.pid or not _platform.pid_alive(session.pid):
            raise ValueError("only live terminal Claude sessions can be steered")
        tty = session.tty or find_tty(session.pid)
    else:
        raise ValueError("this session source does not support steering")
    if not tty:
        raise ValueError("session is not mapped to an exact terminal tab")
    return session.pid, tty


def send_instruction(session: Session, instruction: object) -> dict:
    _require_steer(session)
    exact = validate_instruction(instruction)
    pid, tty = _terminal_target(session)
    result = _platform.send_terminal_text(pid, tty, exact)
    if not result.ok:
        raise RuntimeError(result.detail or "terminal send failed")
    return {"ok": True, "action": "send", "key": session.key, "target": result.target}


def interrupt_session(session: Session) -> dict:
    _require_steer(session)
    pid, tty = _terminal_target(session)
    result = _platform.interrupt_terminal(pid, tty)
    if not result.ok:
        raise RuntimeError(result.detail or "terminal interrupt failed")
    return {"ok": True, "action": "interrupt", "key": session.key, "target": result.target}


@dataclass(frozen=True)
class PendingAction:
    confirmation_id: str
    session_key: str
    session_id: str
    action: str
    instruction: str | None
    summary: str
    created_at: float


class ConfirmationStore:
    """Short-lived, one-use, process-local confirmations for steering."""

    def __init__(self, now: Callable[[], float] = time.time):
        self._now = now
        self._pending: dict[str, PendingAction] = {}

    def _purge(self) -> None:
        cutoff = self._now() - CONFIRMATION_TTL_S
        self._pending = {
            key: pending
            for key, pending in self._pending.items()
            if pending.created_at >= cutoff
        }
        while len(self._pending) >= MAX_CONFIRMATIONS:
            oldest = min(self._pending, key=lambda key: self._pending[key].created_at)
            del self._pending[oldest]

    def create(self, session: Session, action: str, instruction: object = None) -> PendingAction:
        _require_steer(session)
        if action == "send":
            exact = validate_instruction(instruction)
            summary = f"Send this exact line to @{session.name}: {json.dumps(exact)}"
        elif action == "interrupt":
            exact = None
            summary = f"Send Ctrl-C to @{session.name}"
        else:
            raise ValueError("unsupported steering action")
        # Resolve the exact tab before asking for confirmation, then resolve it
        # again at execution time. A preview for an uncontrollable card is a
        # dead-end affordance and should fail immediately.
        _terminal_target(session)
        self._purge()
        confirmation_id = secrets.token_urlsafe(24)
        pending = PendingAction(
            confirmation_id=confirmation_id,
            session_key=session.key,
            session_id=session.session_id,
            action=action,
            instruction=exact,
            summary=summary,
            created_at=self._now(),
        )
        self._pending[confirmation_id] = pending
        return pending

    def consume(self, confirmation_id: object) -> PendingAction:
        if not isinstance(confirmation_id, str) or len(confirmation_id) > 128:
            raise ValueError("invalid confirmation ID")
        self._purge()
        pending = self._pending.pop(confirmation_id, None)
        if pending is None:
            raise ValueError("confirmation is missing, expired, or already used")
        return pending


def execute_pending(pending: PendingAction, session: Session) -> dict:
    if session.key != pending.session_key or session.session_id != pending.session_id:
        raise ValueError("session changed after preview; request a new confirmation")
    if pending.action == "send":
        return send_instruction(session, pending.instruction)
    if pending.action == "interrupt":
        return interrupt_session(session)
    raise ValueError("unsupported steering action")


__all__ = [
    "AUTHORITY_LEVELS",
    "ConfirmationStore",
    "authority_for",
    "execute_pending",
    "interrupt_session",
    "send_instruction",
    "set_authority",
    "validate_instruction",
]
