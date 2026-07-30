"""Static contract for the privacy-safe dashboard demo."""
from __future__ import annotations

import unittest
from pathlib import Path


APP_JS = Path(__file__).parents[1] / "huginn" / "server" / "static" / "app.js"


class DemoDashboardTests(unittest.TestCase):
    def test_demo_renders_a_worktree_contention_from_its_roster(self):
        source = APP_JS.read_text(encoding="utf-8")

        self.assertGreaterEqual(source.count('cwd: "/Users/demo/Projects/atlas"'), 2)
        self.assertIn("function demoTriage(roster)", source)
        self.assertIn("setTriage(demoTriage(roster));", source)


if __name__ == "__main__":
    unittest.main()
