"""Codex subagent counts (issue #8) -- thread_spawn_edges is empty on any
machine that hasn't used Codex's spawn-subagent feature yet, so this seeds
the real schema in a temp sqlite file rather than relying on live data."""
from __future__ import annotations

import sqlite3
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from huginn.config import Config
from huginn.sources import codex
from huginn.sources.codex import _subagent_counts, activity_heartbeat, scan_with_status

DDL = """
CREATE TABLE thread_spawn_edges (
    parent_thread_id TEXT NOT NULL,
    child_thread_id TEXT NOT NULL PRIMARY KEY,
    status TEXT NOT NULL
);
CREATE INDEX idx_thread_spawn_edges_parent_status
    ON thread_spawn_edges(parent_thread_id, status);
"""


class CodexSubagentTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.executescript(DDL)

    def test_groups_children_by_status(self):
        self.conn.executemany(
            "INSERT INTO thread_spawn_edges VALUES (?, ?, ?)",
            [("parent-1", "child-a", "running"),
             ("parent-1", "child-b", "running"),
             ("parent-1", "child-c", "completed"),
             ("parent-2", "child-d", "completed")],
        )
        self.assertEqual(_subagent_counts(self.conn, "parent-1"),
                         {"running": 2, "completed": 1})
        self.assertEqual(_subagent_counts(self.conn, "parent-2"), {"completed": 1})

    def test_no_children_returns_none(self):
        self.assertIsNone(_subagent_counts(self.conn, "lonely-thread"))

    def test_missing_table_returns_none(self):
        conn = sqlite3.connect(":memory:")   # no DDL applied
        self.assertIsNone(_subagent_counts(conn, "any"))


class CodexScanStatusTests(unittest.TestCase):
    def test_backup_destination_is_closed_before_atomic_replace(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.sqlite"
            cache = root / "cache"; cache.mkdir()
            writer = sqlite3.connect(source)
            writer.execute("CREATE TABLE threads (id TEXT PRIMARY KEY)")
            writer.commit(); writer.close()
            reader = codex._connect_ro(source)
            real_connect = sqlite3.connect
            real_replace = os.replace
            destinations = []

            def connect(path):
                destination = real_connect(path)
                destinations.append(destination)
                return destination

            def replace(pending, snapshot):
                with self.assertRaises(sqlite3.ProgrammingError):
                    destinations[0].execute("SELECT 1")
                real_replace(pending, snapshot)

            with patch("huginn.sources.codex.config.CACHE_DIR", cache), \
                 patch("huginn.sources.codex.config.ensure_state_dirs"), \
                 patch("huginn.sources.codex._backup_cache_ts", 0), \
                 patch("huginn.sources.codex.sqlite3.connect", side_effect=connect), \
                 patch("huginn.sources.codex.os.replace", side_effect=replace):
                codex._refresh_backup(reader)
            reader.close()

        self.assertTrue(destinations)

    def test_online_backup_includes_uncheckpointed_wal_transaction(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.sqlite"
            cache = root / "cache"; cache.mkdir()
            writer = sqlite3.connect(source)
            writer.execute("PRAGMA journal_mode=WAL")
            writer.execute("CREATE TABLE threads (id TEXT PRIMARY KEY)")
            writer.execute("INSERT INTO threads VALUES ('visible-from-wal')")
            writer.commit()
            reader = codex._connect_ro(source)
            with patch("huginn.sources.codex.config.CACHE_DIR", cache), \
                 patch("huginn.sources.codex.config.ensure_state_dirs"), \
                 patch("huginn.sources.codex._backup_cache_ts", 0):
                codex._refresh_backup(reader)
            reader.close(); writer.close()
            snapshot = sqlite3.connect(cache / "codex_state.sqlite")
            rows = snapshot.execute("SELECT id FROM threads").fetchall()
            snapshot.close()
        self.assertEqual(rows, [("visible-from-wal",)])

    def test_recent_consistent_snapshot_is_used_when_live_open_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            snapshot = cache / "codex_state.sqlite"
            stored = sqlite3.connect(snapshot)
            stored.execute("CREATE TABLE threads (id TEXT)")
            stored.execute("INSERT INTO threads VALUES ('cached')")
            stored.commit(); stored.close()
            fallback = sqlite3.connect(snapshot)
            with patch("huginn.sources.codex.config.CACHE_DIR", cache), \
                 patch("huginn.sources.codex._connect_ro",
                       side_effect=[sqlite3.OperationalError("blocked"), fallback]):
                conn = codex._connect_with_fallback()
            self.assertIsNotNone(conn)
            self.assertEqual(conn.execute("SELECT id FROM threads").fetchone(), ("cached",))
            conn.close()

    def test_expired_snapshot_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            snapshot = cache / "codex_state.sqlite"
            snapshot.touch()
            old = __import__("time").time() - codex.BACKUP_MAX_AGE_S - 1
            __import__("os").utime(snapshot, (old, old))
            with patch("huginn.sources.codex.config.CACHE_DIR", cache), \
                 patch("huginn.sources.codex._connect_ro",
                       side_effect=sqlite3.OperationalError("blocked")):
                self.assertIsNone(codex._connect_with_fallback())

    def test_unreadable_database_is_not_a_complete_empty_scan(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "missing.sqlite"
            with patch("huginn.sources.codex.STATE_DB", missing):
                sessions, complete = scan_with_status(Config({}))
        self.assertEqual(sessions, [])
        self.assertFalse(complete)

    def test_display_cap_is_still_an_authoritative_scan(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "state.sqlite"
            conn = sqlite3.connect(db)
            conn.execute("""
                CREATE TABLE threads (
                    id TEXT PRIMARY KEY,
                    updated_at_ms INTEGER,
                    recency_at_ms INTEGER,
                    archived INTEGER,
                    thread_source TEXT
                )
            """)
            now_ms = int(__import__("time").time() * 1000)
            conn.executemany(
                "INSERT INTO threads VALUES (?, ?, ?, 0, '')",
                [(f"thread-{i:02d}", now_ms, now_ms) for i in range(51)],
            )
            conn.commit()
            conn.close()
            with patch("huginn.sources.codex.STATE_DB", db):
                sessions, complete = scan_with_status(Config({}))
        self.assertEqual(len(sessions), 50)
        self.assertTrue(complete)


class ActivityHeartbeatTests(unittest.TestCase):
    """issue #19: was defined but never called from anywhere -- now wired
    into `huginn doctor`'s codex section."""

    def test_returns_mtime_when_wal_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            wal = Path(tmp) / "logs_2.sqlite-wal"
            wal.write_text("x")
            expected = wal.stat().st_mtime
            with patch("huginn.sources.codex.LOGS_WAL", wal):
                hb = activity_heartbeat()
        self.assertEqual(hb, expected)

    def test_none_when_wal_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "logs_2.sqlite-wal"
            with patch("huginn.sources.codex.LOGS_WAL", missing):
                self.assertIsNone(activity_heartbeat())


if __name__ == "__main__":
    unittest.main()
