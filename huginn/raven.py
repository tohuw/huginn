"""Huginn's side of the raven protocol: a self-published descriptor and a
declarative menu for the shared status menu bar (issue #40).

The menu bar is a separate project. It discovers participants ("ravens") by
listing a shared directory of JSON descriptors, fetches a menu from each over
loopback, and **renders that menu without interpreting it** -- it draws the
labels we send and hands our action ids back to us unchanged. Nothing here
depends on the host knowing what a Huginn session is, which is exactly what lets
this file change without any change on the host side.

Three rules from that contract drive the code below, and each has already been a
bug somewhere:

**Declare a version range, never an equality.** ``min_api``/``max_api`` describe
the window we speak and the host renders us if it overlaps its own. Comparing for
equality is issue #38 in this repo: one routine bump silently disabled every
participant with nothing on screen to explain it.

**Publish after binding, withdraw on a clean exit.** A descriptor naming a port
that is not yet listening makes a healthy daemon read as unreachable during
startup. A hard kill that skips the withdrawal is still handled -- the host checks
the recorded pid and cross-checks ``started`` -- so a stale file is a survivable
case, not a broken one.

**Own the credential.** We point the host at Huginn's own token file and the
endpoint stays behind the same ``require_local_origin`` + ``require_token`` gate
as every other ``/api`` route. The host never mints or shares a credential, so an
unauthenticated menu endpoint would be our decision to make, and we decline.

Menu text is sanitised here rather than trusted to the host. Session names,
titles, and blurbs derive from directory names, transcripts, and LLM output --
attacker-influenceable text on its way into a desktop menu -- and the host
sanitising them at its end is defence in depth for the host, not permission for
us to emit control characters.

**Lifecycle belongs to the raven, and starting belongs to the OS.** Huginn
publishes ``quit`` and ``restart`` rows, which is what the native macOS and
Windows menu-bar apps offered before the shared host replaced them. They are
ordinary action ids: the host draws the label and posts the id back exactly as it
does for ``focus:claude:1``, and it never learns that one of them ends the process
it is talking to. That is why adding them moved neither ``MAX_API`` nor anything
on the host side.

There is no ``start`` id, and there cannot be one. A stopped daemon has withdrawn
its descriptor, so there is no menu for the row to live in and no process to serve
it -- the row would have to be offered by something that is not running. Asking the
host to spawn us instead would hand a shared menu bar an interpreter path to
execute, which is the shape ``daemon.json``'s ``python``/``repo`` fields already
needed 0600, an ownership check, and a group/world-writable check on every parent
directory to make safe (issue #41 M5), and multiplying that across every raven is
worse than not having a start button. Start-at-login is ``install-agent``'s job,
where the exec path sits with launchd, systemd, or the Run key -- a supervisor
already built to hold one. Note the consequence a user will meet: launchd's
``KeepAlive`` relaunches the daemon after a clean exit, so with the login agent
installed a **Quit** may not stick, which is the same conflict the macOS app
documented and not something a menu row can mediate.

The parts of this that are the *protocol* rather than Huginn's opinion moved to
``corvidae.descriptor`` and ``corvidae.label`` -- issue #42: the shared
state-directory resolution, the atomic 0600 publish, the ownership-checked
withdraw, and the ANSI/C1/bidi label sanitiser were byte-identical in both raven
projects, and the resolution rule in particular fails *silently* when two
participants disagree. What stayed is everything a second raven would not want:
the descriptor's contents, the menu, and the actions. Muninn's descriptor carries
no token and its rows are links, so a shared payload builder or menu shape would
force a form neither project chose.
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any, Iterable

from corvidae.descriptor import (
    STATE_DIR_ENV,
    descriptor_path as _shared_descriptor_path,
    publish_descriptor,
    state_dir,
    withdraw_descriptor,
)
from corvidae.label import MAX_DETAIL, MAX_LABEL, sanitize_label

from . import config
from .llm.context import redact_secrets
from .model import ATTENTION_STATES, Session, SessionState
from .triage import ATTENTION_REASONS, build_triage

# ── Protocol identity ─────────────────────────────────────────────────────────

NAME = "huginn"
DISPLAY = "Huginn"

#: The inclusive protocol window we speak. A range, not an equality -- issue #38.
MIN_API = 1
MAX_API = 1

#: Higher sorts earlier in the shared menu. Huginn is the attention console, so
#: it leads when other ravens are present; nothing here knows which others exist.
HOST_PRIORITY = 100

#: The header our token is presented in -- the same one server/app.py's
#: require_token already reads, so the menu bar authenticates exactly like the
#: dashboard and the CLI do.
TOKEN_HEADER = "X-Huginn-Token"

MENU_ENDPOINT = "/api/menu"
ACTION_ENDPOINT = "/api/menu/action"

#: Environment override for the shared descriptor directory, part of the
#: contract: it is what lets a test harness point a raven and the host at the
#: same alternate location. Bound from corvidae so nothing here can drift from
#: the name the host actually reads.
STATE_DIR_ENV = STATE_DIR_ENV


# ── Descriptor location ───────────────────────────────────────────────────────
#
# ``state_dir`` is corvidae's, imported above and re-exported so
# ``huginn.raven.state_dir`` keeps working. Its resolution order *is* the
# protocol -- ``$RAVENS_STATE_DIR``, then ``%LOCALAPPDATA%\Ravens`` on Windows,
# then ``$XDG_STATE_HOME/ravens`` -- and it honours ``XDG_STATE_HOME`` while
# ``config.STATE_DIR`` does not. That asymmetry is deliberate, not an oversight:
# Huginn's own state directory is Huginn's business and moving it is a
# compatibility change of its own, whereas this directory is *shared*, so
# replicating Huginn's quirk would publish where the host is not looking on any
# machine that sets ``XDG_STATE_HOME`` -- and the failure would be silent, an
# empty menu with nothing to explain it. Shared code rather than a documented
# convention for exactly that reason (issue #42).

def descriptor_path() -> Path:
    """Where Huginn's own descriptor lives, inside the shared directory."""
    return _shared_descriptor_path(NAME)


# ── Text safety ───────────────────────────────────────────────────────────────
#
# The sanitiser itself is ``corvidae.label.sanitize_label`` -- issue #42. It
# mirrors the host's own rules (ANSI/OSC sequences, C0/C1/DEL, bidi overrides and
# zero-width characters, a whitespace collapse) and both raven projects had the
# same regex set, down to including C1 because a lone 0x9b is an alternate CSI
# introducer. "The host will clean it up" is still not a reason to put an ANSI
# escape on the wire: the host defends itself from a hostile raven, while this
# defends Huginn's users from transcript content Huginn is the one that read.

#: Host-side caps, bound from corvidae rather than re-declared here. Emitting more
#: would be silently truncated there, so they are enforced here where the text is
#: still ours to shorten sensibly.
MAX_LABEL = MAX_LABEL
MAX_DETAIL = MAX_DETAIL
MAX_ACTION_ID = 128

#: Per-section caps, chosen well under the host's 50-per-section / 200-total
#: budget so a busy roster is summarised by us rather than cut off by the host
#: mid-section. A menu bar is a glance, not a roster; the console is the roster.
MAX_ATTENTION_ROWS = 15
MAX_SESSION_ROWS = 15
MAX_CONTENTION_ROWS = 5
MAX_ENDED_ROWS = 5

#: Bound on an inbound action body. The id we publish is at most a prefix plus a
#: session key; anything larger is not one of ours.
MAX_ACTION_BODY = 8 * 1024


def safe_text(value: object, limit: int = MAX_LABEL) -> str:
    """Reduce ``value`` to one bounded, printable line fit for a menu label.

    ``corvidae.label.sanitize_label`` plus redaction, in that order and then
    sanitised again. Redaction runs *before* the length cap on purpose: clipping
    first could cut a credential shape in half so the pattern no longer matches,
    leaving a partial secret on screen. That is why the cap is applied here rather
    than passed down -- corvidae is asked for an uncapped clean line first.

    Redaction runs at all because a menu label carries title, blurb, and
    plugin-summary text -- titles arrive straight from ``PUT
    /api/sessions/{key}/title`` and plugin summaries from installed code, neither
    of which has been through the transcript-distillation seam that already
    redacts peek/blurb/Ask text. It is composed here rather than built into
    corvidae because it is Huginn's decision: Muninn sanitises its labels and
    deliberately does not redact them, and a shared sanitiser that redacted
    unconditionally would take that choice away from it (issue #42).
    """
    cleaned = sanitize_label(value, 0)
    if not cleaned:
        return ""
    # Re-sanitised after redaction: the replacement text is ours, but collapsing
    # whitespace again keeps the spacing tidy where a secret was excised.
    cleaned = sanitize_label(redact_secrets(cleaned), limit)
    return cleaned


def _age_label(since: float, now: float) -> str:
    """Compact age, same vocabulary as `huginn roster`'s column."""
    if not since:
        return "-"
    seconds = max(0, int(now - since))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    return f"{seconds // 3600}h"


# ── Action ids ────────────────────────────────────────────────────────────────

FOCUS_PREFIX = "focus:"
DISMISS_PREFIX = "dismiss:"
OPEN_CONSOLE = "open-console"

#: Lifecycle ids. These carry the two things the superseded macOS and Windows
#: menu-bar apps did that the shared host deliberately will not: **Quit Huginn**
#: stopped the daemon and Option-click swapped in **Restart**. Both are now rows
#: Huginn publishes and handles itself.
#:
#: They are ordinary ids, and that is the whole design. The host draws the label
#: and posts the id back exactly as it does for ``focus:claude:1``; it does not
#: know that one of these ends the process it is talking to, and Roost's SPEC.md
#: §10 makes that indistinguishability normative. Nothing about the protocol
#: reserves these words -- which is why adding them needed no version bump and
#: why ``MAX_API`` below is unchanged.
#:
#: There is deliberately **no start id.** A stopped daemon has withdrawn its
#: descriptor, so there is no menu for a "Start Huginn" row to live in and no
#: process to serve it. Starting at login is ``huginn install-agent``'s job
#: (launchd/systemd/Run key), which puts the exec path in the OS supervisor
#: rather than in a menu bar -- see the module docstring's closing note.
QUIT = "quit"
RESTART = "restart"


def _action_id(prefix: str, key: str) -> str:
    """Return an action id for ``key``, or "" when it cannot be one.

    Session keys come from plugin sources as well as Huginn's own scanners, so a
    key can be long or carry text that has no business in an id. An id we cannot
    publish makes the row inert (label, no action) rather than dropping it: a row
    that admits it does nothing is better than a session that silently vanishes
    from the menu while it is still on the dashboard.
    """
    candidate = f"{prefix}{key}"
    if len(candidate) > MAX_ACTION_ID:
        return ""
    if candidate != safe_text(candidate, MAX_ACTION_ID) or any(c in candidate for c in "\r\n"):
        return ""
    return candidate


# ── The menu ──────────────────────────────────────────────────────────────────

def _row(label: str, *, action: str = "", detail: str = "", style: str = "normal") -> dict:
    """One menu row. Only the fields we actually mean are emitted."""
    item: dict[str, Any] = {"label": label}
    if action:
        item["id"] = action
    if detail:
        item["detail"] = detail
    if style != "normal":
        item["style"] = style
    return item


def _topic(session: Session) -> str:
    """A short "what is this about" signal, preferring the most authoritative.

    Same precedence the CLI roster uses: a title the user set or the LLM guessed,
    then a source's own summary, then the blurb. Deliberately never treated as
    evidence of current state -- it is a label, and the state column beside it is
    the truth.
    """
    for candidate in (session.title, session.source_summary, session.blurb):
        text = safe_text(candidate, 70)
        if text:
            return text
    return ""


def _session_row(session: Session, now: float, *, style: str = "normal") -> dict:
    name = safe_text(session.name, 45) or session.source or "session"
    topic = _topic(session)
    label = f"{name}: {topic}" if topic else name
    reason = ATTENTION_REASONS.get(session.state) or session.state.value.replace("_", " ")
    return _row(
        safe_text(label, MAX_LABEL),
        action=_action_id(FOCUS_PREFIX, session.key),
        detail=safe_text(f"{reason} · {_age_label(session.state_since, now)}", MAX_DETAIL),
        style=style,
    )


def _overflow_row(hidden: int) -> dict:
    """An inert row naming what the caps left out.

    Inert on purpose: it has no action, so the host renders it disabled. Silently
    dropping the tail would make a busy roster look smaller than it is.
    """
    return _row(f"+{hidden} more in the console", style="muted")


def build_menu(sessions: Iterable[Session], *, now: float | None = None) -> dict:
    """Return Huginn's whole menu contribution as the host's declarative shape.

    The host does not know what ``focus:codex:abc`` means and must not need to;
    it renders these labels and posts the ids back. Everything Huginn-specific
    lives in this function.
    """
    observed_at = time.time() if now is None else now
    roster = list(sessions)
    triage = build_triage(roster, now=observed_at)

    attention = [s for s in roster if s.state in ATTENTION_STATES]
    # Sorted by the same urgency the dashboard uses, then oldest first: a
    # permission prompt that has been waiting longest is the one to answer.
    attention.sort(key=lambda s: (s.state != SessionState.WAITING_PERMISSION, s.state_since))
    working = sorted(
        (s for s in roster if s.state == SessionState.WORKING),
        key=lambda s: s.state_since,
    )
    ended = sorted(
        (s for s in roster if s.state == SessionState.ENDED),
        key=lambda s: -s.state_since,
    )

    sections: list[dict] = [{
        "id": "status",
        "items": [_row(
            safe_text(triage["verdict"]["headline"], MAX_LABEL),
            style="attention" if triage["verdict"]["level"] in ("attention", "contention") else "muted",
        )],
    }]

    if attention:
        items = [_session_row(s, observed_at, style="attention")
                 for s in attention[:MAX_ATTENTION_ROWS]]
        if len(attention) > MAX_ATTENTION_ROWS:
            items.append(_overflow_row(len(attention) - MAX_ATTENTION_ROWS))
        sections.append({"id": "attention", "title": "Needs attention", "items": items})

    contentions = triage["contentions"]
    if contentions:
        items = []
        for item in contentions[:MAX_CONTENTION_ROWS]:
            # The full label budget: a worktree path is the whole content of this
            # row, and a path clipped to a prefix loses the part that
            # distinguishes it from its siblings.
            worktree = safe_text(item["worktree"], MAX_LABEL) or "unknown worktree"
            names = ", ".join(safe_text(s["name"], 20) for s in item["sessions"])
            # Inert: the contention is about a directory, not a session, so there
            # is nothing to focus. Naming it is the whole value.
            items.append(_row(worktree, detail=safe_text(names, MAX_DETAIL), style="attention"))
        if len(contentions) > MAX_CONTENTION_ROWS:
            items.append(_overflow_row(len(contentions) - MAX_CONTENTION_ROWS))
        sections.append({"id": "contention", "title": "Same worktree", "items": items})

    if working:
        items = [_session_row(s, observed_at) for s in working[:MAX_SESSION_ROWS]]
        if len(working) > MAX_SESSION_ROWS:
            items.append(_overflow_row(len(working) - MAX_SESSION_ROWS))
        sections.append({"id": "working", "title": "Working", "items": items})

    if ended:
        items = [
            _row(
                safe_text(f"Dismiss {safe_text(s.name, 45) or s.source}", MAX_LABEL),
                action=_action_id(DISMISS_PREFIX, s.key),
                detail=safe_text(f"ended · {_age_label(s.state_since, observed_at)}", MAX_DETAIL),
                style="muted",
            )
            for s in ended[:MAX_ENDED_ROWS]
        ]
        if len(ended) > MAX_ENDED_ROWS:
            items.append(_overflow_row(len(ended) - MAX_ENDED_ROWS))
        sections.append({"id": "ended", "title": "Ended", "items": items})

    sections.append({
        "id": "console",
        "items": [_row("Open Console", action=OPEN_CONSOLE)],
    })

    # Lifecycle, last: it is the destructive part of the menu and belongs below
    # everything a user opens the menu to read. Both rows are what the native
    # menu-bar apps offered ("Quit Huginn", and Restart behind Option-click);
    # Restart is a plain row here rather than a hidden alternate because the host
    # renders labels and has no modifier-key vocabulary to hide one behind -- and
    # a menu item nobody can discover is not a replacement for one they could.
    sections.append({
        "id": "lifecycle",
        "items": [
            _row(f"Quit {DISPLAY}", action=QUIT, style="muted"),
            _row(f"Restart {DISPLAY}", action=RESTART, style="muted"),
        ],
    })

    return {
        "api_version": MAX_API,
        # Replaces the descriptor's display name for this render. Kept constant:
        # the count belongs in the badge, and a name that changes shape every
        # poll is harder to read, not more informative.
        "title": DISPLAY,
        # Our own number, which the host shows beside our name and sums across
        # ravens. It is deliberately the same attention count the dashboard tab
        # title and the macOS app already show, so the three never disagree.
        "badge": len(attention),
        "sections": sections,
    }


# ── Actions ───────────────────────────────────────────────────────────────────

def perform_action(daemon: Any, action_id: object) -> dict:
    """Act on an id we published, or refuse it.

    The id is Huginn's own vocabulary round-tripped through a host that does not
    parse it -- but it still arrives over HTTP from another process, so it is
    matched against what we actually issue rather than parsed for meaning. A
    stale id (a session that ended between the menu build and the click) is the
    normal case, not an error: it is refused with a reason and the next refresh
    simply will not offer it again.
    """
    if not isinstance(action_id, str) or not action_id or len(action_id) > MAX_ACTION_ID:
        return {"ok": False, "error": "unknown action"}

    try:
        if action_id == OPEN_CONSOLE:
            return _open_console(daemon)
        if action_id == QUIT:
            return _stop(daemon, restart=False)
        if action_id == RESTART:
            return _stop(daemon, restart=True)
        if action_id.startswith(FOCUS_PREFIX):
            session = daemon.reducer.sessions.get(action_id[len(FOCUS_PREFIX):])
            if session is None:
                return {"ok": False, "error": "session is no longer live"}
            from .focus import focus_session
            return focus_session(session)
        if action_id.startswith(DISMISS_PREFIX):
            return _dismiss(daemon, action_id[len(DISMISS_PREFIX):])
    except Exception as exc:
        # A menu click must never take the daemon down, and the host must never
        # receive an exception string: focus shells out to AppleScript and
        # process tools, and the message could carry a path or transcript
        # fragment. Diagnostics already stores only the class name.
        daemon.diagnostics.error("raven_action", exc)
        return {"ok": False, "error": "action failed"}
    return {"ok": False, "error": "unknown action"}


def _open_console(daemon: Any) -> dict:
    """Open the dashboard with a fresh auth bootstrap, exactly as `huginn open`.

    Deliberately an action rather than the host's ``url`` row: a link row would
    be opened as ``http://127.0.0.1:{port}/`` with no credential, landing on a
    console that cannot talk to its own API, and the alternative -- publishing a
    ``#t=<token>`` url in the menu payload -- would hand Huginn's token to
    another process for no gain. Doing it here keeps the credential inside the
    daemon that owns it.
    """
    import webbrowser
    port = daemon.cfg.get("server", "port")
    webbrowser.open(f"http://127.0.0.1:{port}/#t={daemon.token}")
    return {"ok": True}


def _stop(daemon: Any, *, restart: bool) -> dict:
    """Stop (or restart) this daemon, answering the host before we go.

    The ordering is the substance of this function. ``daemon.request_stop`` only
    *asks* -- it sets uvicorn's ``should_exit`` and returns -- so this returns a
    normal reply that gets serialised and written to the socket, and the process
    unwinds afterwards. Exiting here instead would make a successful quit look to
    the host like an action that failed: it is still holding an open request with
    a 5 s budget, and a dropped connection is indistinguishable from a wedged
    raven.

    It routes through ``request_stop`` rather than signalling or exiting for a
    second reason: that path is uvicorn's graceful shutdown, so the ``finally`` in
    ``Daemon.run`` runs and withdraws the descriptor, ``daemon.json``, and the
    token (issue #43). The macOS app this replaces sent SIGTERM and escalated to
    SIGKILL after a second, which is precisely how those three files got orphaned.

    A daemon that is not serving refuses instead of pretending: the row was drawn
    from a live menu, so this should not happen, but "no server to stop" is a fact
    to report rather than a success to fake.
    """
    if not daemon.request_stop(restart=restart):
        return {"ok": False, "error": "daemon is not serving"}
    return {"ok": True, "restarting" if restart else "stopping": True}


def _dismiss(daemon: Any, key: str) -> dict:
    """Remove one ended session, the same way POST /api/sessions/{key}/dismiss does."""
    session = daemon.reducer.sessions.get(key)
    if session is None:
        return {"ok": False, "error": "session is no longer live"}
    if session.state != SessionState.ENDED:
        return {"ok": False, "error": "only ended sessions can be dismissed"}
    del daemon.reducer.sessions[key]
    daemon.reducer.transitions.pop(key, None)
    daemon.tails.pop(key, None)
    daemon.mark_dirty()
    daemon.bus.broadcast("session.remove", {"key": key})
    return {"ok": True, "dismissed": key}


# ── Publishing ────────────────────────────────────────────────────────────────

def _process_started(pid: int) -> float:
    """Return ``pid``'s start time as epoch seconds, from the OS where possible.

    Not ``time.time()`` at publish time, which is what ``daemon.json`` records:
    the host cross-checks this field against the OS's own record of when the
    process began, with two seconds of slack, so a value taken after snapshot
    restore and plugin startup would make a healthy daemon read as a recycled
    pid. The wall clock is only a fallback for a platform that cannot answer.
    """
    from .platform import platform as _platform
    try:
        actual = _platform.process_start_time(pid)
    except Exception:
        actual = None
    return float(actual) if actual else time.time()


def descriptor_payload(port: int, *, pid: int | None = None, started: float | None = None) -> dict:
    """Build the exact descriptor document the host reads."""
    process_id = os.getpid() if pid is None else pid
    return {
        "api_version": MAX_API,
        "min_api": MIN_API,
        "max_api": MAX_API,
        "name": NAME,
        "display": DISPLAY,
        "pid": process_id,
        "port": port,
        "started": _process_started(process_id) if started is None else started,
        "host_priority": HOST_PRIORITY,
        # Huginn's real API token, mode 0600, rotated on every daemon start. The
        # host reads it fresh per request and sends it only to the port declared
        # above, so rotation needs no coordination.
        "token_path": str(config.TOKEN_PATH),
        "token_header": TOKEN_HEADER,
        # Paths only: the host pins the origin to 127.0.0.1 and the port to the
        # one above, so an endpoint value cannot redirect it elsewhere.
        "endpoints": {"menu": MENU_ENDPOINT, "action": ACTION_ENDPOINT},
    }


def publish(port: int, *, pid: int | None = None, started: float | None = None) -> Path:
    """Write Huginn's descriptor atomically and return where it landed.

    The write itself is ``corvidae.publish_descriptor`` (issue #42), which is
    atomic because the host may read at any moment and must never see a partial
    file, stages the temp file in the same directory so the replace cannot cross a
    filesystem boundary, and lands the file 0600 and the directory 0700. 0600 for
    the same reason ``daemon.json`` is (issue #41): this holds no secret, but it
    names a port and a token path that another process reads and acts on, so
    integrity matters where confidentiality does not. An existing shared directory
    is never retightened -- it belongs to every raven, not to Huginn.

    Only the payload is Huginn's, which is exactly the split: Muninn's descriptor
    carries no token at all.
    """
    return publish_descriptor(
        NAME, descriptor_payload(port, pid=pid, started=started))


def withdraw(pid: int | None = None) -> None:
    """Remove our descriptor if it is still ours, best-effort.

    Ownership is checked inside ``corvidae.withdraw_descriptor``, the same way the
    ``daemon.json`` teardown checks it: a second daemon that lost the port race, or
    a replacement that already republished, must not have its descriptor deleted by
    our exit. If this never runs at all the host still copes, because it verifies
    the recorded pid before trusting the file -- so there is no reason to add
    machinery guaranteeing it. Returns nothing, unlike the shared helper, because
    the daemon's teardown has nothing to do with the answer.
    """
    withdraw_descriptor(NAME, pid=pid)


__all__ = [
    "ACTION_ENDPOINT", "DISMISS_PREFIX", "DISPLAY", "FOCUS_PREFIX", "MAX_ACTION_BODY",
    "MAX_API", "MENU_ENDPOINT", "MIN_API", "NAME", "OPEN_CONSOLE", "QUIT", "RESTART",
    "build_menu", "descriptor_path", "descriptor_payload", "perform_action",
    "publish", "safe_text", "state_dir", "withdraw",
]
