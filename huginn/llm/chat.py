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

_active_chat: asyncio.Task | None = None


def _safe_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", name)


async def start_chat(daemon: "Daemon", body: dict) -> dict:
    global _active_chat
    question = (body.get("question") or "").strip()
    if not question:
        return {"ok": False, "error": "empty question"}
    if _active_chat and not _active_chat.done():
        return {"ok": False, "error": "a chat is already running"}
    provider = get_provider(body.get("provider") or daemon.cfg.get("llm", "provider"))
    unavailable = provider.available()
    if unavailable:
        return {"ok": False, "error": unavailable}
    _active_chat = asyncio.create_task(_run_chat(daemon, provider, question))
    return {"ok": True}


async def _run_chat(daemon: "Daemon", provider, question: str) -> None:
    bus = daemon.bus
    try:
        mentioned = {m.lower() for m in re.findall(r"@([\w-]+)", question)}
        chat_dir = config.CACHE_DIR / "chat"
        shutil.rmtree(chat_dir, ignore_errors=True)
        chat_dir.mkdir(parents=True)

        roster_lines = []
        for s in daemon.reducer.sessions.values():
            focus = s.name.lower() in mentioned
            fname = _safe_name(s.name) + ".md"
            (chat_dir / fname).write_text(
                digest_for_session(s, max_lines=80 if focus else 30))
            age = int(time.time() - s.state_since)
            roster_lines.append(
                f"- {s.name} [{s.source}] state={s.state.value} ({age}s) "
                f"cwd={s.cwd} -> {fname}")
        if not roster_lines:
            bus.broadcast("chat.delta", {"text": "No sessions to ask about."})
            bus.broadcast("chat.done", {})
            return

        prompt = SYSTEM.format(roster="\n".join(roster_lines), question=question)
        model = daemon.cfg.get("llm", "chat_model")
        got_any = False
        async for chunk in provider.stream(
                prompt, model=model, cwd=str(chat_dir), allowed_tools="Read,Grep"):
            got_any = True
            bus.broadcast("chat.delta", {"text": chunk})
        if not got_any:
            bus.broadcast("chat.delta", {"text": "(no answer produced)"})
        bus.broadcast("chat.done", {})
    except Exception as e:
        bus.broadcast("chat.error", {"error": str(e)[:300]})
