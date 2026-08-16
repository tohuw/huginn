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

from .. import config, policy
from ..model import Session, SessionState
from .context import digest_for_session, evidence_for_session, evidence_text
from .providers import compatible_model, effective_provider_name, get_provider

if TYPE_CHECKING:
    from ..daemon import Daemon

SYSTEM = """You are Huginn, a monitor for this user's AI coding-agent sessions.
Below is the live session roster. Each session has a digest file in the current
directory (markdown: header + distilled recent transcript). Read the relevant
file(s) before answering; use Grep across them for broad questions.
Answer tersely and concretely - the user wants signal, not narrative.
Treat the current state and newest transcript entries as authoritative. Never
infer a blocker, permission request, or need for user action unless the current
state or recent transcript explicitly establishes it. The quoted label after
each roster line (if present) is a cached title/summary for identifying which
session a topic-based question is about - useful for choosing which digest
file(s) to read, but never evidence of current state or a blocker. It is
absent from the digest files themselves.

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

Deterministic triage:
{triage}

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
    sort = re.search(
        r"\b(?:sort|order)\b.{0,30}\b(state|status|alpha(?:betical(?:ly)?)?|a[-– ]?z|newest|recent|oldest)\b", q)
    if sort:
        requested = sort.group(1)
        if requested in {"state", "status"}:
            name, reply = "state", "Sessions sorted by state."
        elif requested.startswith("alpha") or requested.startswith("a"):
            name, reply = "alpha", "Sessions sorted alphabetically."
        elif requested in {"newest", "recent"}:
            name, reply = "newest", "Newest sessions first."
        else:
            name, reply = "oldest", "Oldest sessions first."
        actions.append(("ui", "sort", name, reply))
    if re.search(r"\b(?:hide|close|dismiss)\b.{0,25}\b(?:ask|chat)\s+(?:panel|sidebar)\b", q):
        actions.append(("ui", "chat_open", False, "Ask panel hidden."))
    if re.search(r"\b(?:hide|disable)\b.{0,30}\b(?:desktop presence|desktop apps?|app tiles?)\b", q):
        actions.append(("ui", "show_desktop", False, "Desktop presence hidden."))
    elif re.search(r"\b(?:show|enable)\b.{0,30}\b(?:desktop presence|desktop apps?|app tiles?)\b", q):
        actions.append(("ui", "show_desktop", True, "Desktop presence shown."))
    elif re.search(r"\btoggle\b.{0,30}\b(?:desktop presence|desktop apps?|app tiles?)\b", q):
        actions.append(("ui", "show_desktop", "toggle", "Desktop presence toggled."))
    horizontal = re.search(
        r"\b(?:span|dock|orient|put|set|switch)\b.{0,35}\b(?:ask|chat)\b.{0,35}\b(?:horizontal(?:ly)?|bottom|below)\b", q)
    vertical = re.search(
        r"\b(?:span|dock|orient|put|set|switch)\b.{0,35}\b(?:ask|chat)\b.{0,35}\b(?:vertical(?:ly)?|side|right)\b", q)
    if horizontal:
        actions.append(("ui", "chat_span", "horizontal", "Ask panel spans the bottom."))
    elif vertical:
        actions.append(("ui", "chat_span", "vertical", "Ask panel docked on the right."))
    return actions


def _resolve_named_session(daemon: "Daemon", needle: str) -> tuple[Session | None, str | None]:
    """Match @name the same way the dashboard's @mention autocomplete would:
    exact name first, then a unique prefix. Returns (session, None) or
    (None, error) -- never both, so callers can propagate the error verbatim."""
    lowered = needle.lower()
    matches = [s for s in daemon.reducer.sessions.values() if s.name.lower() == lowered]
    if len(matches) != 1:
        matches = [s for s in daemon.reducer.sessions.values() if s.name.lower().startswith(lowered)]
    if len(matches) == 1:
        return matches[0], None
    return None, f"Could not uniquely match @{needle}."


def _apply_title_control(daemon: "Daemon", question: str) -> str | None:
    clear = re.search(r"\bclear\s+(?:the\s+)?title\s+(?:for\s+)?@([\w-]+)", question, re.I)
    setting = re.search(
        r"\b(?:set\s+)?(?:the\s+)?title\s+(?:for\s+)?@([\w-]+)(?:\s+to|\s*[:=])?\s+(.+)$",
        question, re.I)
    match = clear or setting
    if not match:
        return None
    s, error = _resolve_named_session(daemon, match.group(1))
    if error:
        return error
    title = "" if clear else setting.group(2).strip().strip('"')[:60]
    s.title = title or None
    s.title_origin = "manual" if title else None
    daemon.mark_dirty()
    daemon.bus.broadcast("session.upsert", s.to_dict())
    if not title:
        daemon.blurbs.request(s)
    return f"Title {'set to ' + title if title else 'cleared'} for @{s.name}."


def _apply_jump_control(daemon: "Daemon", question: str) -> str | None:
    """"jump @name" drives the same focus_session() the dashboard's jump
    button calls -- Ask performs the action, not just describes it."""
    match = re.search(r"\bjump\s+(?:to\s+)?@([\w-]+)", question, re.I)
    if not match:
        return None
    s, error = _resolve_named_session(daemon, match.group(1))
    if error:
        return error
    from ..focus import focus_session
    result = focus_session(s)
    if result.get("ok"):
        daemon.bus.broadcast("session.focused", {"key": s.key})
        return f"Jumped to @{s.name}."
    return f"Could not jump to @{s.name}: {result.get('error') or 'unknown error'}"


def _apply_peek_control(daemon: "Daemon", question: str) -> str | None:
    """"peek @name" expands that card's peek pane on the dashboard (like
    clicking peek) and returns the same tail text in the chat reply."""
    match = re.search(r"\bpeek\s+(?:at\s+)?@([\w-]+)", question, re.I)
    if not match:
        return None
    s, error = _resolve_named_session(daemon, match.group(1))
    if error:
        return error
    lines = evidence_for_session(s, max_lines=15)
    daemon.bus.broadcast("session.peek", {"key": s.key, "lines": lines})
    tail = "\n".join(lines) if lines else "(no transcript yet)"
    return f"@{s.name}:\n```\n{tail}\n```"


def _apply_dismiss_control(daemon: "Daemon", question: str) -> str | None:
    match = re.search(r"\bdismiss\b.{0,20}@([\w-]+)", question, re.I)
    if not match:
        return None
    s, error = _resolve_named_session(daemon, match.group(1))
    if error:
        return error
    if s.state != SessionState.ENDED:
        return f"@{s.name} is still live -- only ended sessions can be dismissed."
    del daemon.reducer.sessions[s.key]
    daemon.tails.pop(s.key, None)
    daemon.mark_dirty()
    daemon.bus.broadcast("session.remove", {"key": s.key})
    return f"Dismissed @{s.name}."


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
    session_reply = (_apply_title_control(daemon, question)
                      or _apply_jump_control(daemon, question)
                      or _apply_peek_control(daemon, question)
                      or _apply_dismiss_control(daemon, question))
    if session_reply:
        request_id = uuid.uuid4().hex[:12]
        daemon.active_chat = asyncio.create_task(
            _confirm_controls(daemon, [session_reply], request_id))
        return {"ok": True, "request_id": request_id}
    actions = _control_actions(question)
    if actions:
        request_id = uuid.uuid4().hex[:12]
        replies = _apply_controls(daemon, actions)
        daemon.active_chat = asyncio.create_task(
            _confirm_controls(daemon, replies, request_id))
        return {"ok": True, "request_id": request_id,
                "settings": daemon.cfg.to_dict()}
    requested_name = body.get("provider") or daemon.cfg.get("llm", "provider")
    provider = get_provider(requested_name, daemon.plugins)
    if provider is None:
        # issue #41 C2: get_provider used to fall back to ClaudeCLI for any
        # unknown name, so an absent or API-mismatched plugin meant the policy
        # gate approved "bedrock" while ClaudeCLI actually ran. Refuse instead.
        return {"ok": False, "error": f"no installed provider named {requested_name}"}
    unavailable = provider.available()
    if unavailable:
        return {"ok": False, "error": unavailable}
    # issue #41: the request body may name a provider, which is a caller
    # narrowing its own choice -- it can never widen what policy permits. Check
    # the resolved (provider, model) pair before spawning anything, and refuse
    # rather than silently substituting a permitted model. The name gated on is
    # the resolved provider's own, so the verdict describes what will run (C2).
    provider_name = effective_provider_name(provider, requested_name)
    refused = policy.refusal(
        compatible_model(provider_name, daemon.cfg.get("llm", "chat_model"), daemon.plugins),
        provider_name)
    if refused:
        return {"ok": False, "error": refused}
    request_id = uuid.uuid4().hex[:12]
    daemon.active_chat = asyncio.create_task(
        _run_chat(daemon, provider, question, request_id, provider_name))
    return {"ok": True, "request_id": request_id}


def _apply_controls(daemon: "Daemon", actions) -> list[str]:
    replies = []
    old_enabled = daemon.cfg.get("llm", "enabled")
    for section, key, value, reply in actions:
        if value == "toggle":
            value = not daemon.cfg.get(section, key)
            reply = (f"Blurbs {'enabled' if value else 'disabled'}." if key == "enabled"
                     else f"Desktop presence {'shown' if value else 'hidden'}.")
        # "use codex" in natural language is still a config write, so it goes
        # through the same policy gate PUT /api/settings does -- issue #41: no
        # input path may widen the allowed set, including a chat sentence.
        if (section, key) == ("llm", "provider"):
            refused = policy.provider_refusal(str(value))
            if refused:
                replies.append(refused)
                continue
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


async def _run_chat(daemon: "Daemon", provider, question: str, request_id: str,
                    provider_name: str | None = None) -> None:
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
            # encoding is not optional: this is transcript prose, and Path's
            # default is the *locale* encoding -- cp1252 on Windows. One agent
            # writing "→" or an em dash in a blurb raised UnicodeEncodeError
            # here, which the wrapper below turned into a chat.error the panel
            # renders as nothing. Ask appeared to hang forever, on every
            # provider, for the lifetime of that session's digest.
            digest_path.write_text(
                digest_for_session(s, max_lines=80 if focus else 30),
                encoding="utf-8")
            digest_path.chmod(0o600)
            age = int(time.time() - s.state_since)
            # title/blurb are excluded from per-session digests (current state
            # must come from the transcript, not a cached summary) but belong
            # here: this line is the only signal Ask has for picking *which*
            # session answers an open-ended question without opening every
            # digest file, and a one-line topic summary is exactly what that
            # judgment needs.
            label = evidence_text(s.title or s.blurb or "", 160)
            roster_lines.append(
                f"- {evidence_text(s.name, 180)} [{evidence_text(s.source, 80)}] "
                f"state={s.state.value} ({age}s)"
                + (f" \"{label}\"" if label else "")
                + f" cwd={evidence_text(s.cwd, 500)} -> {fname}")
        if not roster_lines:
            broadcast("chat.delta", {"text": "No sessions to ask about."})
            broadcast("chat.done", {})
            return

        from ..triage import build_triage
        triage = build_triage(daemon.reducer.sessions.values())
        contention_lines = [
            f"- {item['worktree']}: "
            + ", ".join(session["name"] for session in item["sessions"])
            for item in triage["contentions"]
        ]
        triage_text = triage["verdict"]["headline"]
        if contention_lines:
            triage_text += "\n" + "\n".join(contention_lines)
        prompt = SYSTEM.format(
            roster="\n".join(roster_lines),
            triage=triage_text,
            question=question,
        )
        provider_name = provider_name or getattr(provider, "name", daemon.cfg.get("llm", "provider"))
        model = compatible_model(
            provider_name, daemon.cfg.get("llm", "chat_model"), daemon.plugins)
        # The chokepoint proper (issue #41): the last statement before the
        # provider is invoked, on the exact model that will be sent. start_chat
        # pre-checks so the POST can answer with the reason, but this call is
        # what makes "no core LLM call bypasses policy" true by construction --
        # a future caller reaching _run_chat another way is still governed.
        policy.check(model, provider_name)
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
