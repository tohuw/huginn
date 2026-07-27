"""Static contract for Ask transcript persistence across a browser refresh."""
from __future__ import annotations

import unittest
from pathlib import Path


APP_JS = Path(__file__).parents[1] / "huginn" / "server" / "static" / "app.js"


class ChatTranscriptPersistenceTests(unittest.TestCase):
    def test_app_js_persists_and_scopes_the_transcript_to_boot_id(self):
        source = APP_JS.read_text(encoding="utf-8")

        self.assertIn("CHAT_TRANSCRIPT_KEY", source)
        self.assertIn("localStorage", source)
        # Persistence must be scoped to the daemon's boot_id (from
        # /api/sessions), not just "browser has a stored transcript" --
        # otherwise a restarted/quit-and-relaunched daemon's dashboard would
        # show a stale conversation instead of starting fresh.
        self.assertIn("data.boot_id", source)
        self.assertIn("syncChatBoot", source)


if __name__ == "__main__":
    unittest.main()
