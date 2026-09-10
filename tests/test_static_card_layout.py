"""Static contracts for stable, readable dashboard cards."""
from __future__ import annotations

import re
import unittest
from pathlib import Path


STATIC = Path(__file__).parents[1] / "huginn" / "server" / "static"
APP_JS = STATIC / "app.js"
STYLE_CSS = STATIC / "style.css"


class CardLayoutTests(unittest.TestCase):
    def test_blurb_expansion_survives_roster_upserts(self):
        source = APP_JS.read_text(encoding="utf-8")

        self.assertIn("const expandedBlurbs = new Set();", source)
        self.assertIn('wrap.classList.toggle("expanded", expandedBlurbs.has(s.key));', source)
        self.assertIn("expandedBlurbs.delete(key);", source)
        self.assertNotIn('wrap.classList.remove("expanded");', source)

    def test_card_view_has_a_larger_summary_and_bottom_action_rail(self):
        css = STYLE_CSS.read_text(encoding="utf-8")

        self.assertIn("-webkit-line-clamp: 4", css)
        self.assertRegex(css, re.compile(r"\.blurb \{[^}]*min-height: 6em;[^}]*max-height: 6em;", re.S))
        self.assertRegex(css, re.compile(r"\.card \{[^}]*display: flex; flex-direction: column;", re.S))
        self.assertRegex(css, re.compile(r"\.actions \{[^}]*margin-top: auto;", re.S))

    def test_card_identity_keeps_two_lines_in_cards_and_one_in_lists(self):
        css = STYLE_CSS.read_text(encoding="utf-8")

        self.assertRegex(
            css,
            re.compile(r"\.name \{[^}]*-webkit-line-clamp: 2;", re.S),
        )
        self.assertRegex(
            css,
            re.compile(
                r"\[data-view=\"list\"\] \.name \{[^}]*text-overflow: ellipsis;"
                r"[^}]*white-space: nowrap;",
                re.S,
            ),
        )

    def test_native_scale_does_not_use_css_zoom(self):
        css = STYLE_CSS.read_text(encoding="utf-8")

        self.assertNotIn("zoom:", css)
        self.assertIn("font-size: 16px", css)
        self.assertIn("text-size-adjust: 100%", css)

    def test_compact_view_is_bookmarkable_and_attention_only(self):
        source = APP_JS.read_text(encoding="utf-8")

        self.assertIn(
            'const COMPACT_MODE = new URLSearchParams(location.search).get("view") === "compact";',
            source,
        )
        self.assertIn('document.body.classList.toggle("compact-view", view === "compact");', source)
        self.assertIn('const visible = view === "compact"', source)
        self.assertIn('[...sessions.values()].filter((s) => s.attention)', source)

    def test_compact_view_removes_chrome_and_enlarges_the_attention_rows(self):
        css = STYLE_CSS.read_text(encoding="utf-8")

        self.assertIn("body.compact-view", css)
        self.assertIn('body.compact-view aside#chat', css)
        self.assertRegex(
            css,
            re.compile(r'#grid\[data-view="compact"\] \.name \{[^}]*font-size: 1\.75rem;', re.S),
        )


if __name__ == "__main__":
    unittest.main()
