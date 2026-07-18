"""SSE stream over the bus."""
from __future__ import annotations

import asyncio
from typing import AsyncIterator

from ..bus import Bus, sse_format

HEARTBEAT_S = 15


async def event_stream(bus: Bus) -> AsyncIterator[str]:
    q = bus.subscribe()
    try:
        while True:
            try:
                msg = await asyncio.wait_for(q.get(), timeout=HEARTBEAT_S)
                yield sse_format(msg)
            except asyncio.TimeoutError:
                yield ": hb\n\n"
    finally:
        bus.unsubscribe(q)
