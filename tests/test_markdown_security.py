"""Static safety contract for the dependency-free Ask Markdown renderer."""
from __future__ import annotations

import unittest
from pathlib import Path


APP_JS = Path(__file__).parents[1] / "huginn" / "server" / "static" / "app.js"


class MarkdownRendererSecurityTests(unittest.TestCase):
    def test_renderer_avoids_html_injection_sinks(self):
        source = APP_JS.read_text(encoding="utf-8")

        self.assertNotIn("innerHTML", source)
        self.assertNotIn("outerHTML", source)
        self.assertNotIn("document.write", source)

    def test_renderer_uses_text_nodes_for_provider_output(self):
        source = APP_JS.read_text(encoding="utf-8")

        self.assertIn("function appendMarkdownText", source)
        self.assertIn("document.createTextNode", source)
        self.assertIn("element.textContent", source)


if __name__ == "__main__":
    unittest.main()
