"""huginn doctor: verify environment, data sources, hooks, and daemon health."""
from __future__ import annotations

import json
import time
import urllib.request
from pathlib import Path

from . import config

TESTED_CLAUDE = (2, 1)
TESTED_CODEX = (0, 145)


def _check(label: str, ok: bool, detail: str = "") -> bool:
    mark = "\033[32m✓\033[0m" if ok else "\033[31m✗\033[0m"
    print(f" {mark} {label}" + (f" — {detail}" if detail else ""))
    return ok


def _warn(label: str, detail: str = "") -> None:
    print(f" \033[33m!\033[0m {label}" + (f" — {detail}" if detail else ""))


def _daemon_session_count(port: int) -> int:
    """Query the authenticated daemon health endpoint."""
    token = config.TOKEN_PATH.read_text().strip()
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/sessions",
        headers={"X-Huginn-Token": token},
    )
    with urllib.request.urlopen(request, timeout=2) as response:
        return len(json.load(response)["sessions"])


def run_doctor() -> int:
    ok = True
    cfg = config.load()

    print("binaries:")
    from .llm.providers import CODEX_BIN, claude_binary
    cb = claude_binary()
    ok &= _check("claude CLI", bool(cb), cb or "not found via `whence -p claude`")
    codex_ok = Path(CODEX_BIN).exists()
    _check("codex (embedded in ChatGPT.app)", codex_ok,
           CODEX_BIN if codex_ok else "ChatGPT.app not installed — codex source disabled")
    if codex_ok:
        _check("codex auth", (Path.home() / ".codex" / "auth.json").exists(),
               "~/.codex/auth.json")

    print("data sources:")
    from .sources import claude_code, codex as codex_src
    ok &= _check("~/.claude/sessions", claude_code.SESSIONS_DIR.is_dir())
    ok &= _check("~/.claude/projects", claude_code.PROJECTS_DIR.is_dir())
    if codex_src.STATE_DB.exists():
        conn = codex_src._connect_with_fallback()
        _check("codex state db readable", conn is not None)
        if conn:
            conn.close()
    else:
        _warn("codex state db absent", str(codex_src.STATE_DB))
    heartbeat = codex_src.activity_heartbeat()
    if heartbeat is None:
        _warn("codex event log heartbeat unavailable", str(codex_src.LOGS_WAL))
    else:
        age = int(time.time() - heartbeat)
        _check("codex event log active", age < 3600, f"last write {age}s ago")

    print("hooks:")
    from .hooks.install import CLAUDE_SETTINGS, CODEX_HOOKS, HOOK_BIN, _has_huginn
    _check("forwarder installed", HOOK_BIN.exists(), str(HOOK_BIN))
    for path in (CLAUDE_SETTINGS, CODEX_HOOKS):
        try:
            hooks = json.loads(path.read_text()).get("hooks", {})
            n = sum(1 for ev in hooks.values() if _has_huginn(ev))
            _check(f"{path.name} hook events", n > 0, f"{n} events wired")
        except (OSError, json.JSONDecodeError):
            _warn(f"{path} unreadable")

    print("versions:")
    for s in claude_code.scan():
        if s.version:
            parts = tuple(int(x) for x in s.version.split(".")[:2])
            if parts > TESTED_CLAUDE:
                _warn(f"claude {s.version} newer than tested {TESTED_CLAUDE}",
                      "parsers are tolerant, but verify states look right")
            break

    print("daemon:")
    daemon_json = config.STATE_DIR / "daemon.json"
    if daemon_json.exists():
        try:
            info = json.loads(daemon_json.read_text())
            n = _daemon_session_count(info["port"])
            _check("daemon running", True, f"port {info['port']}, {n} sessions")
        except Exception:
            _warn("daemon state file present but daemon unreachable",
                  "stale daemon.json or crashed daemon")
    else:
        _warn("daemon not running", "start with `huginn serve`")

    return 0 if ok else 1
