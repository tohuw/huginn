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

    def test_native_scale_does_not_use_css_zoom(self):
        css = STYLE_CSS.read_text(encoding="utf-8")

        self.assertNotIn("zoom:", css)
        self.assertIn("font-size: 16px", css)
        self.assertIn("text-size-adjust: 100%", css)


if __name__ == "__main__":
    unittest.main()
