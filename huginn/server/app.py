"""FastAPI routes. The app is created against a running Daemon instance."""
from __future__ import annotations

import hmac
import json
import time
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .. import config
from ..model import Event, STATE_RANK
from .sse import event_stream

if TYPE_CHECKING:
    from ..daemon import Daemon

STATIC_DIR = Path(__file__).parent / "static"
NOTIFICATIONS_LOG = config.STATE_DIR / "notifications.log"


def _log_notification(source: str, message: str) -> None:
    """Opt-in (patterns.debug_log) raw-message capture for tuning the
    permission/waiting pattern lists against real traffic -- issue #1."""
    if not message:
        return
    try:
        config.ensure_state_dirs()
        with NOTIFICATIONS_LOG.open("a") as f:
            f.write(json.dumps({"ts": time.time(), "source": source, "message": message}) + "\n")
    except OSError:
        pass


def create_app(daemon: "Daemon") -> FastAPI:
    app = FastAPI(title="huginn")
    bus, reducer, cfg = daemon.bus, daemon.reducer, daemon.cfg

    def require_token(request: Request) -> None:
        supplied = request.headers.get("X-Huginn-Token") or request.query_params.get("token") or ""
        if not hmac.compare_digest(supplied, daemon.token):
            raise HTTPException(401, "bad or missing token")

    # A router-level dependency (not @app.middleware("http")) so this doesn't
    # go through Starlette's BaseHTTPMiddleware, which buffers/breaks SSE
    # streaming (StreamingResponse disconnect detection hangs under it).
    api = APIRouter(prefix="/api", dependencies=[Depends(require_token)])

    @app.get("/")
    def index():
        # Same-origin bootstrap: the token rides in the page the browser
        # already trusts (127.0.0.1-only) instead of a separate fetch.
        html = (STATIC_DIR / "index.html").read_text()
        html = html.replace(
            "<script src=\"/static/app.js\"></script>",
            f'<script>const HUGINN_TOKEN = "{daemon.token}";</script>\n'
            '<script src="/static/app.js"></script>',
        )
        return HTMLResponse(html)

    @api.get("/sessions")
    def sessions():
        items = sorted(reducer.sessions.values(),
                       key=lambda s: (STATE_RANK[s.state], s.state_since))
        return {"sessions": [s.to_dict() for s in items],
                "attention": reducer.attention_count()}

    @api.get("/events")
    async def events():
        return StreamingResponse(event_stream(bus), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache"})

    @api.get("/sessions/{key}/tail")
    def tail(key: str, n: int = 15):
        s = reducer.sessions.get(key)
        if s is None:
            raise HTTPException(404)
        from ..llm.context import distill
        return {"lines": distill(s.transcript_path or "", s.source, max_lines=n)}

    @api.post("/sessions/{key}/focus")
    def focus(key: str):
        s = reducer.sessions.get(key)
        if s is None:
            raise HTTPException(404)
        from ..focus import focus_session
        return focus_session(s)

    @api.post("/hook/{source}/{event}")
    async def hook(source: str, event: str, request: Request):
        try:
            data = await request.json()
        except Exception:
            data = {}
        payload = {"event": event, "data": data}
        daemon.record_hook_hit(source, event)
        if event == "Notification" and cfg.section("patterns").get("debug_log"):
            _log_notification(source, data.get("message") or "")
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

    @api.get("/hook-stats")
    def hook_stats():
        """issue #2: which hook events actually fire, per source. Persists
        across restarts (piggybacks on the #7 sessions.json snapshot)."""
        return {"hits": daemon.hook_hits}

    @api.get("/settings")
    def get_settings():
        return cfg.to_dict()

    @api.put("/settings")
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

    @api.post("/chat")
    async def chat(request: Request):
        body = await request.json()
        from ..llm.chat import start_chat
        result = await start_chat(daemon, body)
        if not result.get("ok"):
            raise HTTPException(409, result.get("error") or "chat rejected")
        return result

    app.include_router(api)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app
