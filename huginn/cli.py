"""huginn CLI: status | serve | install-hooks | uninstall-hooks | doctor"""
from __future__ import annotations

import argparse
import sys
import time

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


def cmd_install_hooks(args: argparse.Namespace) -> int:
    from .hooks.install import install
    return install()


def cmd_uninstall_hooks(args: argparse.Namespace) -> int:
    from .hooks.install import uninstall
    return uninstall()


def cmd_doctor(args: argparse.Namespace) -> int:
    from .doctor import run_doctor
    return run_doctor()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="huginn", description="Local AI coding-session monitor")
    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("status", help="one-shot table of sessions")
    sp.add_argument("--all", action="store_true", help="include dead/ended sessions")
    sp.set_defaults(fn=cmd_status)

    sp = sub.add_parser("serve", help="run the daemon + dashboard")
    sp.add_argument("--no-open", action="store_true", help="don't open the browser")
    sp.set_defaults(fn=cmd_serve)

    sub.add_parser("install-hooks", help="install Claude Code + Codex hooks").set_defaults(fn=cmd_install_hooks)
    sub.add_parser("uninstall-hooks", help="remove huginn hooks").set_defaults(fn=cmd_uninstall_hooks)
    sub.add_parser("doctor", help="check environment and configuration").set_defaults(fn=cmd_doctor)

    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
