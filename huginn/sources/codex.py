"""Codex source: ~/.codex/state_5.sqlite thread index + rollout JSONL activity.

The DBs are actively written by the Codex desktop app — always open read-only.
If a direct ro open fails (observed under sandboxing: WAL shm creation is
blocked), fall back to copying db+wal to our cache dir and reading the copy.
"""
from __future__ import annotations

import shutil
import sqlite3
import time
from pathlib import Path

from .. import config
from ..model import Session, SessionState

CODEX_DIR = Path.home() / ".codex"
STATE_DB = CODEX_DIR / "state_5.sqlite"
LOGS_WAL = CODEX_DIR / "logs_2.sqlite-wal"

# Rollout files touched within this window count as "actively working".
ACTIVE_ROLLOUT_S = 60

_THREAD_COLS = [
    "id", "rollout_path", "cwd", "title", "first_user_message", "preview",
    "model", "tokens_used", "git_branch", "updated_at_ms", "recency_at_ms",
    "archived", "source", "thread_source", "agent_nickname", "cli_version",
]

_copy_cache_ts: float = 0.0


def _connect_ro(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=1.0)
    conn.execute("PRAGMA busy_timeout=250")
    return conn


def _connect_with_fallback() -> sqlite3.Connection | None:
    global _copy_cache_ts
    try:
        conn = _connect_ro(STATE_DB)
        conn.execute("SELECT 1 FROM threads LIMIT 1")
        return conn
    except sqlite3.Error:
        pass
    # Copy-to-cache fallback; reuse a copy younger than 5s.
    config.ensure_state_dirs()
    copy = config.CACHE_DIR / "codex_state.sqlite"
    try:
        if time.time() - _copy_cache_ts > 5 or not copy.exists():
            shutil.copy2(STATE_DB, copy)
            wal = STATE_DB.with_name(STATE_DB.name + "-wal")
            wal_copy = copy.with_name(copy.name + "-wal")
            if wal.exists():
                shutil.copy2(wal, wal_copy)
            elif wal_copy.exists():
                wal_copy.unlink()
            _copy_cache_ts = time.time()
        return _connect_ro(copy)
    except (OSError, sqlite3.Error):
        return None


def _available_cols(conn: sqlite3.Connection) -> list[str]:
    have = {row[1] for row in conn.execute("PRAGMA table_info(threads)")}
    return [c for c in _THREAD_COLS if c in have]


def _subagent_counts(conn: sqlite3.Connection, thread_id: str) -> dict[str, int] | None:
    """Group child threads spawned by this one (issue #8) by status."""
    try:
        rows = conn.execute(
            "SELECT status, COUNT(*) FROM thread_spawn_edges "
            "WHERE parent_thread_id=? GROUP BY status",
            (thread_id,),
        ).fetchall()
    except sqlite3.Error:
        return None
    return {status: count for status, count in rows} or None


def scan_with_status(cfg: config.Config | None = None) -> tuple[list[Session], bool]:
    """Scan recent threads and report whether the result is complete.

    ``complete`` is false on database errors and when the 50-card display cap
    truncates the query.  Callers may only reconcile missing sessions after a
    complete scan; an empty failed scan must never erase live state.
    """
    cfg = cfg or config.load()
    if not STATE_DB.exists():
        return [], False
    conn = _connect_with_fallback()
    if conn is None:
        return [], False
    sessions: list[Session] = []
    try:
        cols = _available_cols(conn)
        if "id" not in cols:
            return [], False
        cutoff_ms = int((time.time() - cfg.get("codex", "active_window_h") * 3600) * 1000)
        where = "archived=0 AND COALESCE(updated_at_ms, recency_at_ms, 0) > ?"
        if not cfg.get("codex", "include_subagents") and "thread_source" in cols:
            where += " AND COALESCE(thread_source,'') NOT IN ('subagent')"
        rows = conn.execute(
            f"SELECT {', '.join(cols)} FROM threads WHERE {where} "
            "ORDER BY COALESCE(updated_at_ms, recency_at_ms, 0) DESC LIMIT 51",
            (cutoff_ms,),
        ).fetchall()
        complete = len(rows) <= 50
        rows = rows[:50]
        huginn_dir = str(config.STATE_DIR)
        for row in rows:
            t = dict(zip(cols, row))
            # huginn's own codex exec calls (blurbs/chat run under STATE_DIR)
            # register as threads too — don't monitor ourselves
            if (t.get("cwd") or "").startswith(huginn_dir):
                continue
            sessions.append(_thread_to_session(t, subagents=_subagent_counts(conn, t["id"])))
    except sqlite3.Error:
        return [], False
    finally:
        conn.close()
    return sessions, complete


def scan(cfg: config.Config | None = None) -> list[Session]:
    """One-shot scan of recent, unarchived Codex threads."""
    sessions, _ = scan_with_status(cfg)
    return sessions


def _thread_to_session(t: dict, subagents: dict[str, int] | None = None) -> Session:
    updated_s = (t.get("updated_at_ms") or t.get("recency_at_ms") or 0) / 1000.0
    rollout = t.get("rollout_path")
    rollout_mtime = 0.0
    if rollout:
        try:
            rollout_mtime = Path(rollout).stat().st_mtime
        except OSError:
            rollout = None
    last = max(updated_s, rollout_mtime)
    age = time.time() - last
    if age < ACTIVE_ROLLOUT_S:
        state = SessionState.WORKING
    elif age < 600:
        state = SessionState.DONE
    else:
        state = SessionState.IDLE
    cwd = t.get("cwd") or ""
    name = t.get("agent_nickname") or (Path(cwd).name if cwd else "codex")
    return Session(
        key=f"codex:{t['id']}",
        source="codex",
        session_id=t["id"],
        cwd=cwd,
        name=f"{name}-{t['id'][-4:]}",
        git_branch=t.get("git_branch"),
        model=t.get("model"),
        entrypoint=t.get("source"),
        state=state,
        state_since=last,
        state_origin="poll",
        transcript_path=rollout,
        last_activity=last,
        last_prompt=(t.get("first_user_message") or "")[:300] or None,
        tokens=t.get("tokens_used"),
        version=t.get("cli_version"),
        subagents=subagents,
    )


def activity_heartbeat() -> float | None:
    """mtime of the live event-log WAL — coarse 'codex is doing something' signal."""
    try:
        return LOGS_WAL.stat().st_mtime
    except OSError:
        return None
