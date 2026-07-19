"""Q&A agent over live sessions.

Lazy retrieval: per-session digest files are written to a scratch dir and the
provider runs there with Read/Grep only — the prompt carries a roster, not
whole transcripts. Answers stream to the dashboard over the bus.
"""
from __future__ import annotations

import asyncio
import re
import shutil
import time
import uuid
from typing import TYPE_CHECKING

from .. import config
from .context import digest_for_session
from .providers import get_provider

if TYPE_CHECKING:
    from ..daemon import Daemon

SYSTEM = """You are Huginn, a monitor for this user's AI coding-agent sessions.
Below is the live session roster. Each session has a digest file in the current
directory (markdown: header + distilled recent transcript). Read the relevant
file(s) before answering; use Grep across them for broad questions.
Answer tersely and concretely - the user wants signal, not narrative.
Treat the current state and newest transcript entries as authoritative. Never
infer a blocker, permission request, or need for user action unless the current
state or recent transcript explicitly establishes it. Cached dashboard blurbs
are not evidence and are intentionally absent from the digest files.

Your scope is exclusively the agent sessions in this roster: their state,
activity, output, blockers, errors, and what needs the user's attention. Do not
answer unrelated general-knowledge, advice, coding, writing, cooking, or other
assistant requests. For an out-of-scope question, reply briefly that Huginn only
monitors agent sessions and gently suggest asking the user's full Claude or
Codex agent instead. Do not answer the out-of-scope question itself.

Example: "How do I make lasagna?" is out of scope. Respond along the lines of:
"I only monitor your agent sessions. Ask your full Claude or Codex agent for
that." Do not provide a recipe.

Roster:
{roster}

Question: {question}
"""


def _safe_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", name)


def _control_actions(question: str) -> list[tuple[str, str, object, str]]:
    """Recognize the small, explicit set of dashboard controls Ask owns."""
    q = question.lower()
    actions: list[tuple[str, str, object, str]] = []
    if "blurb" in q:
        if re.search(r"\b(?:disable|hide)\b.{0,25}\bblurbs?\b|\bblurbs?\b.{0,25}\boff\b", q):
            actions.append(("llm", "enabled", False, "Blurbs disabled."))
        elif re.search(r"\b(?:enable|show)\b.{0,25}\bblurbs?\b|\bblurbs?\b.{0,25}\bon\b", q):
            actions.append(("llm", "enabled", True, "Blurbs enabled."))
        elif "toggle" in q:
            actions.append(("llm", "enabled", "toggle", "Blurbs toggled."))
    provider = re.search(
        r"\b(?:use|switch|set|change|prefer)\b.{0,40}\b(claude|codex)\b", q)
    if provider:
        name = provider.group(1)
        actions.append(("llm", "provider", name, f"Ask agent set to {name}."))
    view = re.search(
        r"\b(?:use|switch|set|change|show|restore)\b.{0,30}\b(list|cards?)\b(?:\s+view)?", q)
    if view:
        name = "cards" if view.group(1).startswith("card") else "list"
        actions.append(("ui", "view", name, f"{name.title()} view enabled."))
    if re.search(r"\b(?:hide|close|dismiss)\b.{0,25}\b(?:ask|chat)\s+(?:panel|sidebar)\b", q):
        actions.append(("ui", "chat_open", False, "Ask panel hidden."))
    return actions


async def start_chat(daemon: "Daemon", body: dict) -> dict:
    # Concurrency is per-daemon, not per-client (issue #17): one chat
    # subprocess in flight at a time, whole-daemon-wide, so a second tab
    # asking a question while one is already running gets rejected rather
    # than spawning a competing LLM process. request_id lets multiple
    # *browser tabs* subscribed to the same SSE stream tell "my answer"
    # apart from "someone else's" -- see app.js's currentRequestId check.
    question = (body.get("question") or "").strip()
    if not question:
        return {"ok": False, "error": "empty question"}
    if daemon.active_chat and not daemon.active_chat.done():
        return {"ok": False, "error": "a chat is already running"}
    actions = _control_actions(question)
    if actions:
        request_id = uuid.uuid4().hex[:12]
        replies = _apply_controls(daemon, actions)
        daemon.active_chat = asyncio.create_task(
            _confirm_controls(daemon, replies, request_id))
        return {"ok": True, "request_id": request_id,
                "settings": daemon.cfg.to_dict()}
    provider = get_provider(body.get("provider") or daemon.cfg.get("llm", "provider"))
    unavailable = provider.available()
    if unavailable:
        return {"ok": False, "error": unavailable}
    request_id = uuid.uuid4().hex[:12]
    daemon.active_chat = asyncio.create_task(_run_chat(daemon, provider, question, request_id))
    return {"ok": True, "request_id": request_id}


def _apply_controls(daemon: "Daemon", actions) -> list[str]:
    replies = []
    old_enabled = daemon.cfg.get("llm", "enabled")
    for section, key, value, reply in actions:
        if value == "toggle":
            value = not daemon.cfg.get(section, key)
            reply = f"Blurbs {'enabled' if value else 'disabled'}."
        daemon.cfg.update(section, key, value)
        replies.append(reply)
    config.save(daemon.cfg)
    new_enabled = daemon.cfg.get("llm", "enabled")
    if new_enabled != old_enabled:
        daemon.blurbs.set_enabled(new_enabled)
    daemon.bus.broadcast("settings.changed", daemon.cfg.to_dict())
    return replies


async def _confirm_controls(daemon: "Daemon", replies: list[str], request_id: str) -> None:
    # Let POST /chat deliver request_id before the SSE confirmation arrives.
    await asyncio.sleep(0.05)
    daemon.bus.broadcast("chat.delta", {
        "request_id": request_id, "text": " ".join(replies)})
    daemon.bus.broadcast("chat.done", {"request_id": request_id})


async def _run_chat(daemon: "Daemon", provider, question: str, request_id: str) -> None:
    bus = daemon.bus

    def broadcast(event: str, data: dict) -> None:
        bus.broadcast(event, {**data, "request_id": request_id})

    # Digest files carry distilled transcript content -- private dir/files
    # regardless of umask, and removed unconditionally below (issue #24):
    # success, provider failure, or task cancellation must all clean up.
    chat_dir = config.CACHE_DIR / "chat"
    try:
        mentioned = {m.lower() for m in re.findall(r"@([\w-]+)", question)}
        config.ensure_state_dirs()
        shutil.rmtree(chat_dir, ignore_errors=True)
        chat_dir.mkdir(parents=True)
        chat_dir.chmod(0o700)

        roster_lines = []
        for s in daemon.reducer.sessions.values():
            focus = s.name.lower() in mentioned
            fname = _safe_name(s.name) + ".md"
            digest_path = chat_dir / fname
            digest_path.write_text(digest_for_session(s, max_lines=80 if focus else 30))
            digest_path.chmod(0o600)
            age = int(time.time() - s.state_since)
            roster_lines.append(
                f"- {s.name} [{s.source}] state={s.state.value} ({age}s) "
                f"cwd={s.cwd} -> {fname}")
        if not roster_lines:
            broadcast("chat.delta", {"text": "No sessions to ask about."})
            broadcast("chat.done", {})
            return

        prompt = SYSTEM.format(roster="\n".join(roster_lines), question=question)
        model = daemon.cfg.get("llm", "chat_model")
        got_any = False
        async for chunk in provider.stream(
                prompt, model=model, cwd=str(chat_dir), allowed_tools="Read,Grep"):
            got_any = True
            broadcast("chat.delta", {"text": chunk})
        if not got_any:
            broadcast("chat.delta", {"text": "(no answer produced)"})
        broadcast("chat.done", {})
    except Exception as e:
        broadcast("chat.error", {"error": str(e)[:300]})
    finally:
        shutil.rmtree(chat_dir, ignore_errors=True)
