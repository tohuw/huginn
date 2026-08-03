"""huginn CLI: status | serve | open | demo | hooks | doctor"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from . import config
from .model import STATE_RANK, Session

_STATE_COLORS = {
    "waiting_permission": "\033[1;35m",
    "waiting_input": "\033[1;33m",
    "error": "\033[1;31m",
    "done": "\033[1;32m",
    "working": "\033[36m",
    "idle": "\033[2m",
    "ended": "\033[2m",
}
_RESET = "\033[0m"


def _age(ts: float) -> str:
    if not ts:
        return "-"
    s = int(time.time() - ts)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m{s % 60:02d}s"
    return f"{s // 3600}h{(s % 3600) // 60:02d}m"


def cmd_status(args: argparse.Namespace) -> int:
    from .sources import claude_code, codex

    cfg = config.load()
    sessions: list[Session] = claude_code.scan(include_dead=args.all)
    sessions += codex.scan(cfg)
    sessions.sort(key=lambda s: (STATE_RANK[s.state], -s.last_activity))

    if not sessions:
        print("no sessions found")
        return 0

    color = sys.stdout.isatty()
    rows = [("NAME", "SOURCE", "STATE", "AGE", "MODEL", "BRANCH", "CWD")]
    for s in sessions:
        rows.append((
            s.name, s.source, s.state.value, _age(s.state_since),
            s.model or "-", s.git_branch or "-", s.cwd or "-",
        ))
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]) - 1)]
    for i, r in enumerate(rows):
        line = "  ".join(c.ljust(w) for c, w in zip(r[:-1], widths)) + "  " + r[-1]
        if color and i > 0:
            line = _STATE_COLORS.get(r[2], "") + line + _RESET
        print(line)
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    from .daemon import run
    return run(config.load(), open_browser=not args.no_open)


def cmd_open(args: argparse.Namespace) -> int:
    """Reopen the dashboard with a fresh auth bootstrap, without restarting
    the daemon -- the token rides in a URL fragment, never sent to the
    server (see issue #23). This remains useful for a new browser profile or
    after browser cookies have been cleared."""
    import webbrowser
    if not (config.STATE_DIR / "daemon.json").exists():
        print("huginn: daemon not running (try `huginn serve`)")
        return 1
    port = (config.STATE_DIR / "port").read_text().strip()
    token = config.TOKEN_PATH.read_text().strip()
    webbrowser.open(f"http://127.0.0.1:{port}/#t={token}")
    return 0


def cmd_demo(args: argparse.Namespace) -> int:
    """Open the self-contained fictional dashboard without live roster data."""
    import webbrowser
    if not (config.STATE_DIR / "port").exists():
        print("huginn: daemon not running (try `huginn serve`)")
        return 1
    port = (config.STATE_DIR / "port").read_text().strip()
    webbrowser.open(f"http://127.0.0.1:{port}/?demo=1")
    return 0


def _daemon_api(
    path: str,
    method: str = "GET",
    body: dict | None = None,
    timeout: float = 3,
) -> dict:
    """Call the local authenticated daemon without exposing token mechanics."""
    if not (config.STATE_DIR / "daemon.json").exists():
        # Names only things that still exist: Huginn.app is deleted, and the shared
        # menu bar deliberately cannot start a stopped daemon, so pointing a user
        # at a menu here would be pointing at nothing.
        raise RuntimeError("daemon not running (try `huginn serve`)")
    try:
        port = (config.STATE_DIR / "port").read_text().strip()
        token = config.TOKEN_PATH.read_text().strip()
        data = json.dumps(body).encode() if body is not None else None
        headers = {"X-Huginn-Token": token}
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}{path}", method=method,
            headers=headers, data=data)
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.load(response)
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as e:
        raise RuntimeError(f"cannot reach Huginn daemon: {type(e).__name__}") from e


def _live_sessions(attention_only: bool = False) -> list[dict]:
    sessions = _daemon_api("/api/sessions")["sessions"]
    if attention_only:
        sessions = [s for s in sessions if s.get("attention")]
    return sessions


def _resolve_session(target: str, sessions: list[dict]) -> dict:
    needle = target.removeprefix("@").lower()
    exact = [s for s in sessions if s["name"].lower() == needle or s["key"].lower() == needle]
    if len(exact) == 1:
        return exact[0]
    partial = [s for s in sessions if s["name"].lower().startswith(needle)]
    if len(partial) == 1:
        return partial[0]
    if not exact and not partial:
        raise RuntimeError(f"no live session matches {target!r}")
    names = ", ".join(s["name"] for s in (exact or partial))
    raise RuntimeError(f"ambiguous session {target!r}: {names}")


def cmd_roster(args: argparse.Namespace) -> int:
    try:
        sessions = _live_sessions(args.attention)
    except RuntimeError as e:
        print(f"huginn: {e}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps({"sessions": sessions}, indent=2))
        return 0
    if not sessions:
        print("no sessions need attention" if args.attention else "no live sessions")
        return 0
    for s in sessions:
        summary = (s.get("blurb") or s.get("last_prompt") or "").replace("\n", " ")[:100]
        if s.get("shells"):
            n = s["shells"]
            summary = f"[{n} shell{'s' if n != 1 else ''}] {summary}"
        print(f"@{s['name']}\t{s['state']}\t{_age(s['state_since'])}\t"
              f"{s['source']}\t{summary}\t{s.get('cwd') or '-'}")
    return 0


def cmd_triage(args: argparse.Namespace) -> int:
    try:
        result = _daemon_api("/api/sessions").get("triage", {})
    except RuntimeError as e:
        print(f"huginn: {e}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(result, indent=2))
        return 0
    verdict = result.get("verdict", {})
    print(verdict.get("headline") or "No triage result")
    for item in result.get("contentions", []):
        names = ", ".join(f"@{session['name']}" for session in item.get("sessions", []))
        print(f"  {item.get('worktree')}: {names}")
    return 0


def cmd_inspect(args: argparse.Namespace) -> int:
    if not args.attention and not args.target:
        print("huginn: inspect requires @name or --attention", file=sys.stderr)
        return 2
    try:
        sessions = _live_sessions(args.attention)
        selected = sessions if args.attention else [_resolve_session(args.target, sessions)]
        details = []
        for s in selected:
            key = urllib.parse.quote(s["key"], safe="")
            tail = _daemon_api(f"/api/sessions/{key}/tail?n={args.lines}").get("lines", [])
            details.append({**s, "tail": tail})
    except RuntimeError as e:
        print(f"huginn: {e}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps({"sessions": details}, indent=2))
        return 0
    if not details:
        print("no sessions need attention")
        return 0
    for i, s in enumerate(details):
        if i:
            print("\n---\n")
        print(f"@{s['name']} [{s['source']}] {s['state']} ({_age(s['state_since'])})")
        print(f"cwd: {s.get('cwd') or '-'}")
        if s.get("shells"):
            print(f"shells: {s['shells']} running")
        if s.get("blurb"):
            print(f"summary: {s['blurb']}")
        elif s.get("last_prompt"):
            print(f"prompt: {s['last_prompt']}")
        print("\nRecent activity:")
        print("\n".join(s["tail"]) or "(no transcript yet)")
    return 0


def cmd_history(args: argparse.Namespace) -> int:
    """State-transition history for one session -- issue: a card seen
    briefly in the wrong state (e.g. a codex poll crossing a staleness
    threshold) self-corrects before you can screenshot it. This is the
    evidence trail that survives that."""
    try:
        session = _resolve_session(args.target, _live_sessions())
        key = urllib.parse.quote(session["key"], safe="")
        transitions = _daemon_api(f"/api/sessions/{key}/transitions").get("transitions", [])
    except RuntimeError as e:
        print(f"huginn: {e}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps({"key": session["key"], "transitions": transitions}, indent=2))
        return 0
    if not transitions:
        print(f"no recorded transitions for @{session['name']}")
        return 0
    for t in transitions:
        when = time.strftime("%H:%M:%S", time.localtime(t["ts"]))
        print(f"{when}  {t['from']:>18} -> {t['to']:<18} ({t['origin']})")
    return 0


def cmd_focus(args: argparse.Namespace) -> int:
    try:
        session = _resolve_session(args.target, _live_sessions())
        key = urllib.parse.quote(session["key"], safe="")
        result = _daemon_api(f"/api/sessions/{key}/focus", method="POST")
    except RuntimeError as e:
        print(f"huginn: {e}", file=sys.stderr)
        return 1
    if not result.get("ok", True):
        print(f"huginn: focus failed: {result.get('error') or result}", file=sys.stderr)
        return 1
    print(f"focused @{session['name']}")
    return 0


def cmd_authority(args: argparse.Namespace) -> int:
    try:
        session = _resolve_session(args.target, _live_sessions())
        key = urllib.parse.quote(session["key"], safe="")
        result = _daemon_api(
            f"/api/sessions/{key}/authority",
            method="PUT",
            body={"level": args.level},
        )
    except RuntimeError as e:
        print(f"huginn: {e}", file=sys.stderr)
        return 1
    print(f"@{session['name']} authority: {result['level']}")
    return 0


def _confirmed_steering(target: str, action: str, instruction: str | None = None) -> int:
    try:
        session = _resolve_session(target, _live_sessions())
        key = urllib.parse.quote(session["key"], safe="")
        preview = _daemon_api(
            f"/api/sessions/{key}/steering/preview",
            method="POST",
            body={"action": action, "instruction": instruction},
        )
        answer = input(f"{preview['summary']}\nType 'yes' to confirm: ")
        confirmed = answer.strip().lower() == "yes"
        result = _daemon_api(
            "/api/steering/confirm",
            method="POST",
            body={
                "confirmation_id": preview["confirmation_id"],
                "confirmed": confirmed,
            },
            timeout=12,
        )
    except (EOFError, KeyboardInterrupt):
        print("\nhuginn: steering cancelled", file=sys.stderr)
        return 1
    except RuntimeError as e:
        print(f"huginn: {e}", file=sys.stderr)
        return 1
    if not confirmed or result.get("cancelled"):
        print("huginn: steering cancelled", file=sys.stderr)
        return 1
    print(f"{action} confirmed for @{session['name']}")
    return 0


def cmd_send(args: argparse.Namespace) -> int:
    return _confirmed_steering(args.target, "send", " ".join(args.instruction))


def cmd_interrupt(args: argparse.Namespace) -> int:
    return _confirmed_steering(args.target, "interrupt")


def cmd_install_hooks(args: argparse.Namespace) -> int:
    from .hooks.install import install
    return install()


def cmd_uninstall_hooks(args: argparse.Namespace) -> int:
    from .hooks.install import uninstall
    return uninstall()


def cmd_doctor(args: argparse.Namespace) -> int:
    from .doctor import run_doctor
    return run_doctor()


def cmd_install_agent(args: argparse.Namespace) -> int:
    from .agent_install import install
    return install()


def cmd_uninstall_agent(args: argparse.Namespace) -> int:
    from .agent_install import uninstall
    return uninstall()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="huginn", description="Local AI coding-session monitor")
    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("status", help="one-shot table of sessions")
    sp.add_argument("--all", action="store_true", help="include dead/ended sessions")
    sp.set_defaults(fn=cmd_status)

    sp = sub.add_parser("serve", help="run the daemon + dashboard")
    sp.add_argument("--no-open", action="store_true", help="don't open the browser")
    sp.set_defaults(fn=cmd_serve)

    sub.add_parser("open", help="reopen the dashboard with a fresh auth bootstrap").set_defaults(fn=cmd_open)
    sub.add_parser("demo", help="open an interactive fictional dashboard").set_defaults(fn=cmd_demo)

    sp = sub.add_parser("roster", help="compact live roster for agents and scripts")
    sp.add_argument("--attention", action="store_true", help="only sessions needing user attention")
    sp.add_argument("--json", action="store_true", help="emit structured JSON")
    sp.set_defaults(fn=cmd_roster)

    sp = sub.add_parser("triage", help="summarize attention and worktree contention")
    sp.add_argument("--json", action="store_true", help="emit structured JSON")
    sp.set_defaults(fn=cmd_triage)

    sp = sub.add_parser("inspect", help="read a distilled live-session digest")
    sp.add_argument("target", nargs="?", help="session name, @name, or canonical key")
    sp.add_argument("--attention", action="store_true", help="inspect every session needing attention")
    sp.add_argument("--lines", type=int, default=30, choices=range(1, 201), metavar="N")
    sp.add_argument("--json", action="store_true", help="emit structured JSON")
    sp.set_defaults(fn=cmd_inspect)

    sp = sub.add_parser("focus", help="focus a live session by name")
    sp.add_argument("target", help="session name, @name, or canonical key")
    sp.set_defaults(fn=cmd_focus)

    sp = sub.add_parser("history", help="show a session's recorded state-transition history")
    sp.add_argument("target", help="session name, @name, or canonical key")
    sp.add_argument("--json", action="store_true", help="emit structured JSON")
    sp.set_defaults(fn=cmd_history)

    sp = sub.add_parser("authority", help="set observe or steer authority for one session")
    sp.add_argument("target", help="session name, @name, or canonical key")
    sp.add_argument("level", choices=("observe", "steer"))
    sp.set_defaults(fn=cmd_authority)

    sp = sub.add_parser("send", help="confirm and send one exact line to a steer-authorized session")
    sp.add_argument("target", help="session name, @name, or canonical key")
    sp.add_argument("instruction", nargs="+", help="one line of terminal input")
    sp.set_defaults(fn=cmd_send)

    sp = sub.add_parser("interrupt", help="confirm Ctrl-C for a steer-authorized session")
    sp.add_argument("target", help="session name, @name, or canonical key")
    sp.set_defaults(fn=cmd_interrupt)

    sub.add_parser("install-hooks", help="install Claude Code + Codex hooks").set_defaults(fn=cmd_install_hooks)
    sub.add_parser("uninstall-hooks", help="remove huginn hooks").set_defaults(fn=cmd_uninstall_hooks)
    sub.add_parser("doctor", help="check environment and configuration").set_defaults(fn=cmd_doctor)

    sub.add_parser("install-agent", help="start huginn at login (launchd/systemd/Windows)").set_defaults(fn=cmd_install_agent)
    sub.add_parser("uninstall-agent", help="stop starting huginn at login").set_defaults(fn=cmd_uninstall_agent)

    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
