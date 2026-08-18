"""Static contract for dashboard rendering of plugin session groups."""
from __future__ import annotations

import unittest
from pathlib import Path


APP_JS = Path(__file__).parents[1] / "huginn" / "server" / "static" / "app.js"
STYLE_CSS = Path(__file__).parents[1] / "huginn" / "server" / "static" / "style.css"
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

    def test_app_js_offers_and_persists_plugin_secondary_sort(self):
        source = APP_JS.read_text(encoding="utf-8")

        self.assertIn("s.group_sort_key", source)
        self.assertIn("s.group_sort_label", source)
        self.assertIn("compareGroupSortKey(a, b) || compare(a, b)", source)
        self.assertIn("group_sorts", source)
        self.assertIn("sort by ${groupSortLabel}", source)
        self.assertIn('boundary.className = "plugin-group-boundary"', source)
        self.assertIn("sortCounts.get(sortKey)", source)
        self.assertIn("boundaryLabel.textContent", source)
        self.assertIn("boundaryCount.textContent", source)

    def test_member_boundaries_span_the_group_grid(self):
        source = STYLE_CSS.read_text(encoding="utf-8")

        self.assertIn(".plugin-group-boundary", source)
        self.assertIn("grid-column: 1 / -1", source)

    def test_app_js_renders_plugin_source_label_on_the_card_badge(self):
        source = APP_JS.read_text(encoding="utf-8")

        self.assertIn("s.source_label", source)
        self.assertIn("sourceLabel", source)


if __name__ == "__main__":
    unittest.main()
