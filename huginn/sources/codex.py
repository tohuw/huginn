"""Codex source: ~/.codex/state_5.sqlite thread index + rollout JSONL activity.

The DB is actively written by Codex — always open read-only.  A successful
read refreshes a transactionally consistent SQLite online-backup snapshot;
when sandboxing temporarily blocks the live WAL/shm, a recent snapshot is a
safe fallback.  Never copy the live database files directly.
"""
from __future__ import annotations

import contextlib
import sqlite3
import os
import time
from pathlib import Path

from .. import config
from ..model import Session, SessionState
from ..platform import platform as _platform

CODEX_DIR = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
STATE_DB = CODEX_DIR / "state_5.sqlite"
LOGS_WAL = CODEX_DIR / "logs_2.sqlite-wal"

# Rollout files touched within this window count as "actively working".
# A single tool call or reasoning burst under a slow/high-effort model can
# leave the rollout file untouched for several minutes without the session
# actually being done -- observed gaps up to ~210s on gpt-5.6-sol at xhigh
# effort. 240s clears that with margin while staying well under the 600s
# DONE->IDLE boundary below.
ACTIVE_ROLLOUT_S = 240

_THREAD_COLS = [
    "id", "rollout_path", "cwd", "title", "first_user_message", "preview",
    "model", "tokens_used", "git_branch", "updated_at_ms", "recency_at_ms",
    "archived", "source", "thread_source", "agent_nickname", "cli_version",
]

_backup_cache_ts: float = 0.0
BACKUP_MAX_AGE_S = 30.0
BACKUP_REFRESH_S = 5.0


def cli_terminal_alive(session: Session) -> bool:
    """Whether a Codex CLI process still owns a TTY in this workspace.

    Codex's thread database does not record the CLI pid.  Matching the live
    process by cwd is deliberately conservative: if two threads share a
    workspace we may retain an old card, which is preferable to dropping the
    card for an open terminal tab.
    """
    if session.entrypoint != "cli" or not session.cwd:
        return False
    for pid in _platform.find_processes("codex"):
        if (_platform.pid_alive(pid)
                and _platform.process_cwd(pid) == session.cwd
                and _platform.process_tty(pid)):
            return True
    return False


def _connect_ro(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=1.0)
    conn.execute("PRAGMA busy_timeout=250")
    return conn


def _refresh_backup(source: sqlite3.Connection) -> None:
    """Atomically publish a consistent snapshot using sqlite3_backup()."""
    global _backup_cache_ts
    config.ensure_state_dirs()
    snapshot = config.CACHE_DIR / "codex_state.sqlite"
    if snapshot.exists() and time.time() - _backup_cache_ts <= BACKUP_REFRESH_S:
        return
    pending = snapshot.with_suffix(".sqlite.pending")
    try:
        pending.unlink(missing_ok=True)
        # sqlite3.Connection's context manager commits or rolls back but does
        # not close the handle. POSIX permits replacing that open file;
        # Windows correctly rejects it with ERROR_SHARING_VIOLATION.
        destination = sqlite3.connect(pending)
        try:
            source.backup(destination)
        finally:
            destination.close()
        os.replace(pending, snapshot)
        _backup_cache_ts = time.time()
    except (OSError, sqlite3.Error):
        with contextlib.suppress(OSError):
            pending.unlink(missing_ok=True)


def _connect_with_fallback() -> sqlite3.Connection | None:
    conn: sqlite3.Connection | None = None
    try:
        conn = _connect_ro(STATE_DB)
        conn.execute("SELECT 1 FROM threads LIMIT 1")
        _refresh_backup(conn)
        return conn
    except sqlite3.Error:
        if conn is not None:
            conn.close()
    snapshot = config.CACHE_DIR / "codex_state.sqlite"
    try:
        if time.time() - snapshot.stat().st_mtime > BACKUP_MAX_AGE_S:
            return None
        return _connect_ro(snapshot)
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
    """Scan recent threads and report whether the query succeeded.

    The boolean is false only when the database could not be read.  Crossing
    the recency window or 50-card display cap is an authoritative roster miss,
    but the daemon separately checks terminal liveness before evicting CLI
    cards because an open tab matters more than database recency.
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
        rows = rows[:50]
        huginn_dir = str(config.STATE_DIR)
        for row in rows:
            t = dict(zip(cols, row))
            # huginn's own codex exec calls (blurbs/chat run under STATE_DIR)
            # register as threads too — don't monitor ourselves
            if (t.get("cwd") or "").startswith(huginn_dir):
                continue
            session = _thread_to_session(t, subagents=_subagent_counts(conn, t["id"]))
            # The dashboard is a live roster, not a second thread-history UI.
            # Codex keeps completed top-level threads unarchived, including
            # short `codex exec` probes launched from another agent's scratchpad.
            if session.state == SessionState.IDLE:
                continue
            if session.state == SessionState.DONE:
                ttl = cfg.get("ui", "exec_done_ttl_s") if session.entrypoint == "exec" \
                    else cfg.get("ui", "done_ttl_s")
                if time.time() - session.last_activity >= ttl:
                    continue
            sessions.append(session)
    except sqlite3.Error:
        return [], False
    finally:
        conn.close()
    return sessions, True


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
