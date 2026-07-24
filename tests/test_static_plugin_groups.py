"""Static contract for dashboard rendering of Session.group/group_label."""
from __future__ import annotations

import unittest
from pathlib import Path


APP_JS = Path(__file__).parents[1] / "huginn" / "server" / "static" / "app.js"
INDEX_HTML = Path(__file__).parents[1] / "huginn" / "server" / "static" / "index.html"


class PluginGroupRenderingTests(unittest.TestCase):
    def test_index_has_a_plugin_groups_mount_point(self):
        self.assertIn('id="plugin-groups"', INDEX_HTML.read_text(encoding="utf-8"))

    def test_app_js_renders_and_persists_group_visibility(self):
        source = APP_JS.read_text(encoding="utf-8")

        self.assertIn("getOrCreatePluginGroupSection", source)
        self.assertIn("s.group", source)
        self.assertIn("s.group_label", source)
        self.assertIn("hidden_groups", source)


if __name__ == "__main__":
    unittest.main()
