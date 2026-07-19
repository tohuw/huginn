"""Codex subagent counts (issue #8) -- thread_spawn_edges is empty on any
machine that hasn't used Codex's spawn-subagent feature yet, so this seeds
the real schema in a temp sqlite file rather than relying on live data."""
from __future__ import annotations

import sqlite3
import unittest

from huginn.sources.codex import _subagent_counts

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


if __name__ == "__main__":
    unittest.main()
