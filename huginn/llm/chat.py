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

Roster:
{roster}

Question: {question}
"""


def _safe_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", name)


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
    provider = get_provider(body.get("provider") or daemon.cfg.get("llm", "provider"))
    unavailable = provider.available()
    if unavailable:
        return {"ok": False, "error": unavailable}
    request_id = uuid.uuid4().hex[:12]
    daemon.active_chat = asyncio.create_task(_run_chat(daemon, provider, question, request_id))
    return {"ok": True, "request_id": request_id}


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
