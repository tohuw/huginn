"""FastAPI routes. The app is created against a running Daemon instance."""
from __future__ import annotations

import time
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .. import config
from ..model import Event, STATE_RANK
from .sse import event_stream

if TYPE_CHECKING:
    from ..daemon import Daemon

STATIC_DIR = Path(__file__).parent / "static"


def create_app(daemon: "Daemon") -> FastAPI:
    app = FastAPI(title="huginn")
    bus, reducer, cfg = daemon.bus, daemon.reducer, daemon.cfg

    @app.get("/")
    def index():
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/api/sessions")
    def sessions():
        items = sorted(reducer.sessions.values(),
                       key=lambda s: (STATE_RANK[s.state], s.state_since))
        return {"sessions": [s.to_dict() for s in items],
                "attention": reducer.attention_count()}

    @app.get("/api/events")
    async def events():
        return StreamingResponse(event_stream(bus), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache"})

    @app.get("/api/sessions/{key}/tail")
    def tail(key: str, n: int = 15):
        s = reducer.sessions.get(key)
        if s is None:
            raise HTTPException(404)
        from ..llm.context import distill
        return {"lines": distill(s.transcript_path or "", s.source, max_lines=n)}

    @app.post("/api/sessions/{key}/focus")
    def focus(key: str):
        s = reducer.sessions.get(key)
        if s is None:
            raise HTTPException(404)
        from ..focus import focus_session
        return focus_session(s)

    @app.post("/api/hook/{source}/{event}")
    async def hook(source: str, event: str, request: Request):
        try:
            data = await request.json()
        except Exception:
            data = {}
        payload = {"event": event, "data": data}
        if source == "claude" and event == "Stop":
            # Disambiguate DONE vs WAITING_INPUT from the transcript tail.
            sid = data.get("session_id", "")
            s = reducer.find_by_session_id(sid)
            if s is not None:
                pair = daemon.tails.get(s.key)
                if pair:
                    tail_obj, analyzer = pair
                    analyzer.feed(tail_obj.read_new())
                    payload["asked_question"] = getattr(analyzer, "asked_user_question", False)
        bus.emit(Event(f"hook.{source}", None, time.time(), "hook", payload))
        return {"ok": True}

    @app.get("/api/settings")
    def get_settings():
        return cfg.to_dict()

    @app.put("/api/settings")
    async def put_settings(request: Request):
        body = await request.json()
        for section, values in body.items():
            if section not in config.DEFAULTS or not isinstance(values, dict):
                continue
            for key, value in values.items():
                if key in config.DEFAULTS[section]:
                    cfg.update(section, key, value)
        config.save(cfg)
        return cfg.to_dict()

    @app.post("/api/chat")
    async def chat(request: Request):
        body = await request.json()
        from ..llm.chat import start_chat
        return await start_chat(daemon, body)

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app
