"""FastAPI routes. The app is created against a running Daemon instance."""
from __future__ import annotations

import hmac
import json
import time
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .. import config
from ..model import Event, STATE_RANK, SessionState
from ..steering import authority_for, execute_pending, set_authority
from ..triage import build_triage
from .sse import event_stream

if TYPE_CHECKING:
    from ..daemon import Daemon
    from ..diagnostics import Diagnostics

STATIC_DIR = Path(__file__).parent / "static"
NOTIFICATIONS_LOG = config.STATE_DIR / "notifications.log"
NOTIFICATIONS_LOG_MAX_BYTES = 2 * 1024 * 1024
NOTIFICATIONS_LOG_KEEP_LINES = 2000
SESSION_COOKIE = "huginn_token"
REFRESH_COOKIE = "huginn_refresh"


def _rotate_notifications_log() -> None:
    """Bound retention: an opt-in debug log left on for weeks shouldn't grow
    forever (issue #24). Keeps the newest lines, drops the rest."""
    try:
        if NOTIFICATIONS_LOG.stat().st_size <= NOTIFICATIONS_LOG_MAX_BYTES:
            return
    except OSError:
        return
    lines = NOTIFICATIONS_LOG.read_text(encoding="utf-8").splitlines()[-NOTIFICATIONS_LOG_KEEP_LINES:]
    NOTIFICATIONS_LOG.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


def _log_notification(source: str, message: str, diagnostics: "Diagnostics") -> None:
    """Opt-in (patterns.debug_log) raw-message capture for tuning the
    permission/waiting pattern lists against real traffic -- issue #1."""
    if not message:
        return
    try:
        config.ensure_state_dirs()
        _rotate_notifications_log()
        with NOTIFICATIONS_LOG.open("a") as f:
            f.write(json.dumps({"ts": time.time(), "source": source, "message": message}) + "\n")
        NOTIFICATIONS_LOG.chmod(0o600)   # regardless of umask
        diagnostics.ok("notifications_log")
    except OSError as e:
        diagnostics.error("notifications_log", e)


def _secret_matches(supplied: object, expected: object) -> bool:
    """Constant-time compare of two credentials that cannot be made to raise.

    ``hmac.compare_digest`` refuses two ``str`` arguments unless both are
    ASCII-only, and it raises TypeError rather than returning False. A header is
    whatever the caller put in it, so one non-ASCII character turned a request
    that should have been a plain 401 into a 500 with a traceback in the log --
    reachable by anyone who can reach the port, without a token. Comparing the
    encoded bytes compares what actually arrived.
    """
    if not isinstance(supplied, str) or not isinstance(expected, str) or not expected:
        return False
    return hmac.compare_digest(
        supplied.encode("utf-8", "surrogateescape"),
        expected.encode("utf-8", "surrogateescape"),
    )


def create_app(daemon: "Daemon") -> FastAPI:
    app = FastAPI(title="huginn")
    bus, reducer, cfg = daemon.bus, daemon.reducer, daemon.cfg

    port = cfg.get("server", "port")

    def require_token(request: Request) -> None:
        # No query-param fallback: a bearer token must never ride in a URL
        # (browser history, access logs). SSE authenticates via the cookie
        # set by POST /api/session instead -- see issue #23.
        supplied = request.headers.get("X-Huginn-Token") or request.cookies.get(SESSION_COOKIE) or ""
        if not _secret_matches(supplied, daemon.token):
            raise HTTPException(401, "bad or missing token")

    def require_local_origin(request: Request) -> None:
        # Defends against DNS-rebinding / malicious-webpage requests: a page
        # served from any other hostname will show that hostname in Host,
        # even if it resolves to 127.0.0.1.
        host = (request.headers.get("host") or "").split(":")[0]
        if host not in ("127.0.0.1", "localhost"):
            raise HTTPException(400, "unexpected host")
        origin = request.headers.get("origin")
        if origin is not None and origin not in (
                f"http://127.0.0.1:{port}", f"http://localhost:{port}"):
            raise HTTPException(403, "cross-origin request rejected")

    # Router-level dependencies (not @app.middleware("http")) so this doesn't
    # go through Starlette's BaseHTTPMiddleware, which buffers/breaks SSE
    # streaming (StreamingResponse disconnect detection hangs under it).
    api = APIRouter(prefix="/api",
                    dependencies=[Depends(require_local_origin), Depends(require_token)])

    @app.get("/")
    def index():
        # The shell carries no secret: GET / must be safely fetchable by any
        # local process. The token only ever reaches the browser via a URL
        # fragment (never sent to the server) -- see huginn open / `serve`.
        return FileResponse(STATIC_DIR / "index.html")

    @api.post("/session")
    def create_session(response: Response):
        # Called once by app.js after it pulls the token out of the URL
        # fragment. Establishes an HttpOnly cookie so subsequent requests
        # (including EventSource, which can't set custom headers) never need
        # to put the token in a URL or JS-readable storage again.
        response.set_cookie(SESSION_COOKIE, daemon.token, httponly=True,
                            samesite="strict", path="/")
        response.set_cookie(REFRESH_COOKIE, daemon.refresh_token, httponly=True,
                            samesite="strict", path="/api/session/refresh")
        return {"ok": True}

    @app.post("/api/session/refresh", dependencies=[Depends(require_local_origin)])
    def refresh_session(request: Request, response: Response):
        """Rotate an already-authorized browser tab onto this daemon's token."""
        supplied = request.cookies.get(REFRESH_COOKIE) or ""
        if not _secret_matches(supplied, daemon.refresh_token):
            raise HTTPException(401, "bad or missing refresh token")
        response.set_cookie(SESSION_COOKIE, daemon.token, httponly=True,
                            samesite="strict", path="/")
        return {"ok": True}

    @api.get("/sessions")
    def sessions():
        items = sorted(reducer.sessions.values(),
                       key=lambda s: (STATE_RANK[s.state], s.state_since))
        return {"sessions": [s.to_dict() for s in items],
                "attention": reducer.attention_count(),
                "triage": build_triage(items),
                "boot_id": daemon.boot_id,
                # Whether absence from `sessions` is evidence. False during
                # startup, when a source simply has not looked yet; the console
                # reconciles removals only when this is true, so a slow boot
                # cannot blank a roster it is still assembling.
                "complete": daemon.roster_complete()}

    @api.get("/activity")
    def activity():
        """Probe live sources during the browser's empty-roster startup gap."""
        if reducer.sessions:
            return {"agents_running": True}
        from ..sources import chatgpt_desktop, claude_code, codex
        try:
            if chatgpt_desktop.scan():
                return {"agents_running": True}
        except Exception as e:
            daemon.diagnostics.error("activity_chatgpt_desktop", e)
        try:
            if claude_code.scan():
                return {"agents_running": True}
        except Exception as e:
            daemon.diagnostics.error("activity_claude", e)
        try:
            codex_sessions, _ = codex.scan_with_status(cfg)
            if codex_sessions:
                return {"agents_running": True}
        except Exception as e:
            daemon.diagnostics.error("activity_codex", e)
        return {"agents_running": False}

    @api.get("/events")
    async def events():
        return StreamingResponse(event_stream(bus), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache"})

    @api.get("/sessions/{key}/tail")
    def tail(key: str, n: int = 15):
        s = reducer.sessions.get(key)
        if s is None:
            raise HTTPException(404)
        from ..llm.context import evidence_for_session
        return {"lines": evidence_for_session(s, max_lines=max(1, min(n, 100)))}

    @api.get("/sessions/{key}/transitions")
    def transitions(key: str):
        """Bounded state-change history (huginn.state.MAX_TRANSITION_HISTORY
        entries) so a card seen briefly in the wrong state -- e.g. a poll
        crossing a staleness threshold and correcting on the next cycle --
        leaves evidence after the fact instead of vanishing once it flips
        back."""
        if key not in reducer.sessions:
            raise HTTPException(404)
        return {"key": key, "transitions": list(reducer.transitions.get(key, []))}

    @api.post("/sessions/{key}/focus")
    def focus(key: str):
        s = reducer.sessions.get(key)
        if s is None:
            raise HTTPException(404)
        from ..focus import focus_session
        return focus_session(s)

    @api.post("/sessions/{key}/dismiss")
    def dismiss(key: str):
        s = reducer.sessions.get(key)
        if s is None:
            raise HTTPException(404)
        if s.state != SessionState.ENDED:
            raise HTTPException(409, "only ended sessions can be dismissed")
        del reducer.sessions[key]
        reducer.transitions.pop(key, None)
        daemon.tails.pop(key, None)
        daemon.mark_dirty()
        bus.broadcast("session.remove", {"key": key})
        return {"ok": True}

    @api.get("/sessions/{key}/authority")
    def get_authority(key: str):
        s = reducer.sessions.get(key)
        if s is None:
            raise HTTPException(404)
        return {"key": s.key, "session_id": s.session_id, "level": authority_for(s)}

    @api.put("/sessions/{key}/authority")
    async def put_authority(key: str, request: Request):
        s = reducer.sessions.get(key)
        if s is None:
            raise HTTPException(404)
        try:
            body = await request.json()
            level = body.get("level") if isinstance(body, dict) else None
            return set_authority(s, level)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @api.post("/sessions/{key}/steering/preview")
    async def preview_steering(key: str, request: Request):
        s = reducer.sessions.get(key)
        if s is None:
            raise HTTPException(404)
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError("request body must be an object")
            pending = daemon.steering_confirmations.create(
                s,
                body.get("action"),
                body.get("instruction"),
            )
        except PermissionError as exc:
            raise HTTPException(403, str(exc)) from exc
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(422, str(exc)) from exc
        return {
            "confirmation_id": pending.confirmation_id,
            "summary": pending.summary,
            "expires_in": 60,
        }

    @api.post("/steering/confirm")
    async def confirm_steering(request: Request):
        try:
            body = await request.json()
            if not isinstance(body, dict) or not isinstance(body.get("confirmed"), bool):
                raise ValueError("confirmed must be a boolean")
            pending = daemon.steering_confirmations.consume(body.get("confirmation_id"))
            if not body["confirmed"]:
                return {"ok": False, "cancelled": True}
            s = reducer.sessions.get(pending.session_key)
            if s is None:
                raise ValueError("session ended after preview")
            return execute_pending(pending, s)
        except PermissionError as exc:
            raise HTTPException(403, str(exc)) from exc
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(422, str(exc)) from exc

    @api.put("/sessions/{key}/title")
    async def set_title(key: str, request: Request):
        s = reducer.sessions.get(key)
        if s is None:
            raise HTTPException(404)
        body = await request.json()
        title = str(body.get("title") or "").strip()[:60]
        s.title = title or None
        s.title_origin = "manual" if title else None
        daemon.mark_dirty()
        bus.broadcast("session.upsert", s.to_dict())
        if not title:
            daemon.blurbs.request(s)
        return s.to_dict()

    @api.post("/hook/{source}/{event}")
    async def hook(source: str, event: str, request: Request):
        try:
            data = await request.json()
        except Exception:
            data = {}
        payload = {"event": event, "data": data}
        daemon.record_hook_hit(source, event)
        if event == "Notification" and cfg.section("patterns").get("debug_log"):
            _log_notification(source, data.get("message") or "", daemon.diagnostics)
        if source == "claude" and event in ("Stop", "Notification"):
            # Disambiguate from the transcript tail: Stop is DONE vs
            # WAITING_INPUT; Notification is a real permission prompt vs an
            # AskUserQuestion (which Claude reports permission-shaped).
            sid = data.get("session_id", "")
            s = reducer.find_by_session_id(sid)
            if s is not None:
                if not s.transcript_path and data.get("transcript_path"):
                    s.transcript_path = data["transcript_path"]
                daemon.ensure_tail(s)
                pair = daemon.tails.get(s.key)
                if pair:
                    tail_obj, analyzer = pair
                    for entries in tail_obj.read_available():
                        analyzer.feed(entries)
                    payload["asked_question"] = getattr(analyzer, "asked_user_question", False)
        bus.emit(Event(f"hook.{source}", None, time.time(), "hook", payload))
        return {"ok": True}

    @api.get("/menu")
    def menu():
        """Declarative menu for the shared status menu bar -- issue #40.

        Behind the same require_local_origin + require_token gate as every other
        /api route, on purpose: the menu bar authenticates by reading Huginn's
        own token from the token_path its descriptor advertises, so there is no
        reason for this route to be the one exception. See huginn/raven.py for
        the contract and why the text is sanitised on this side too."""
        from ..raven import build_menu
        return build_menu(reducer.sessions.values())

    @api.post("/menu/action")
    async def menu_action(request: Request):
        """Act on an action id this daemon itself published in GET /api/menu.

        The host does not interpret the id -- it hands back exactly what we sent
        -- but it still arrives over HTTP, so raven.perform_action matches it
        against what Huginn actually issues rather than parsing it for meaning.
        An unknown or stale id is refused with ok=false and HTTP 200: it is a
        request that could not be honoured, not a malformed one, and the host
        renders the menu again on its next poll regardless."""
        from ..raven import MAX_ACTION_BODY, perform_action
        # Bounded before parsing: a menu click carries an id of at most a
        # hundred-odd bytes, so anything larger is not one of ours.
        raw = await request.body()
        if len(raw) > MAX_ACTION_BODY:
            raise HTTPException(413, "request body is too large")
        try:
            body = json.loads(raw or b"{}")
        except ValueError as exc:
            raise HTTPException(400, "body is not JSON") from exc
        if not isinstance(body, dict):
            raise HTTPException(400, "expected an object with a string id")
        return perform_action(daemon, body.get("id"))

    @api.get("/hook-stats")
    def hook_stats():
        """issue #2: which hook events actually fire, per source. Persists
        across restarts (piggybacks on the #7 sessions.json snapshot)."""
        return {"hits": daemon.hook_hits}

    @api.get("/health")
    def health():
        """issue #15: per-source last-success/last-error, so a source going
        dark shows up here instead of the dashboard just looking stale.
        Redacted by construction -- Diagnostics never stores exception
        message text, only the class name and counts."""
        return {
            "sources": daemon.diagnostics.snapshot(),
            "automatic_text": daemon.blurbs.status(),
        }

    @api.get("/settings")
    def get_settings():
        return cfg.to_dict()

    @api.get("/providers")
    def providers():
        from .. import policy
        from ..llm.providers import all_providers, blurb_model as resolve_blurb_model
        result = {}
        for name, provider in all_providers(daemon.plugins).items():
            # issue #41: an installed policy that forbids this provider makes it
            # unavailable here, with the policy's reason verbatim, so the
            # dashboard never offers a choice the chokepoint would refuse. The
            # policy check comes first: a forbidden provider must read as
            # forbidden even when its binary is also missing.
            reason = policy.provider_refusal(name) or provider.available()
            automatic_model = None
            if reason is None:
                try:
                    candidate = resolve_blurb_model(
                        name, cfg.get("llm", "blurb_model"), daemon.plugins)
                    # A refused automatic model is reported as no model, never
                    # swapped for a permitted one -- see policy.py's rule 2.
                    refused = policy.refusal(candidate, name)
                    automatic_model = None if refused else candidate
                except Exception:
                    pass
            result[name] = {
                "available": reason is None,
                "reason": reason,
                "label": str(getattr(provider, "label", name))[:80],
                "automatic_model": automatic_model,
            }
        return {"providers": result}

    @api.get("/plugins")
    def plugins():
        return daemon.plugins.to_dict()

    @api.put("/settings")
    async def put_settings(request: Request):
        body = await request.json()
        # Validate the whole batch before mutating anything (issue #18) --
        # a partially-invalid update must not poison runtime/disk config.
        errors: list[str] = []
        for section, values in body.items():
            if not isinstance(values, dict):
                errors.append(f"{section}: expected an object")
                continue
            for key, value in values.items():
                err = config.validate_setting(section, key, value)
                if err:
                    errors.append(err)
        if errors:
            raise HTTPException(422, {"errors": errors})
        old_llm_enabled = cfg.get("llm", "enabled")
        old_automatic_provider = (
            cfg.get("llm", "provider"),
            cfg.get("llm", "blurb_model"),
        )
        for section, values in body.items():
            for key, value in values.items():
                cfg.update(section, key, value)
        config.save(cfg)
        new_llm_enabled = cfg.get("llm", "enabled")
        if new_llm_enabled != old_llm_enabled:
            daemon.blurbs.set_enabled(new_llm_enabled)
        elif new_llm_enabled and old_automatic_provider != (
            cfg.get("llm", "provider"),
            cfg.get("llm", "blurb_model"),
        ):
            daemon.blurbs.set_enabled(True)
        bus.broadcast("settings.changed", cfg.to_dict())
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
