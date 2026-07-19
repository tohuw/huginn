"""bus fan-out + SSE formatting, exercised directly (no HTTP layer, no hang risk)."""
from __future__ import annotations

import asyncio
import unittest

from huginn.bus import Bus
from huginn.server.sse import event_stream


class SSETests(unittest.TestCase):
    def test_broadcast_reaches_stream(self):
        async def run():
            bus = Bus()
            gen = event_stream(bus)
            first = asyncio.ensure_future(gen.__anext__())
            await asyncio.sleep(0)   # let event_stream() reach bus.subscribe()
            bus.broadcast("session.upsert", {"key": "claude:1"})
            chunk = await asyncio.wait_for(first, timeout=2)
            self.assertIn("event: session.upsert", chunk)
            self.assertIn("claude:1", chunk)
            await gen.aclose()
        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
