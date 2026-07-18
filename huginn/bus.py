"""Event plumbing: one reducer input queue, fan-out to SSE subscribers."""
from __future__ import annotations

import asyncio
import json
from typing import Any

from .model import Event


class Bus:
    def __init__(self) -> None:
        self.events: asyncio.Queue[Event] = asyncio.Queue()
        self._subscribers: set[asyncio.Queue[dict]] = set()

    # -- reducer input ----------------------------------------------------
    def emit(self, event: Event) -> None:
        self.events.put_nowait(event)

    # -- SSE fan-out ------------------------------------------------------
    def subscribe(self) -> asyncio.Queue[dict]:
        q: asyncio.Queue[dict] = asyncio.Queue(maxsize=500)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[dict]) -> None:
        self._subscribers.discard(q)

    def broadcast(self, event_name: str, data: Any) -> None:
        msg = {"event": event_name, "data": data}
        for q in list(self._subscribers):
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                self.unsubscribe(q)   # slow client; it will reconnect and resnapshot


def sse_format(msg: dict) -> str:
    return f"event: {msg['event']}\ndata: {json.dumps(msg['data'])}\n\n"
