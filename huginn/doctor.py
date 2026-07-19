"""huginn doctor: verify environment, data sources, hooks, and daemon health."""
from __future__ import annotations

import json
import time
import urllib.request
from pathlib import Path

from . import config

# Fixture provenance in tests/fixtures/PROVENANCE.md -- bump these (and the
# fixtures) together when upgrading, per issue #22's "surface versions newer
# than fixture coverage" ask.
TESTED_CLAUDE = (2, 1)
TESTED_CODEX = (0, 144)


def _check(label: str, ok: bool, detail: str = "") -> bool:
    mark = "\033[32m✓\033[0m" if ok else "\033[31m✗\033[0m"
    print(f" {mark} {label}" + (f" — {detail}" if detail else ""))
    return ok


def _warn(label: str, detail: str = "") -> None:
    print(f" \033[33m!\033[0m {label}" + (f" — {detail}" if detail else ""))


def _authed_get(port: int, path: str) -> dict:
    token = config.TOKEN_PATH.read_text().strip()
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        headers={"X-Huginn-Token": token},
    )
    with urllib.request.urlopen(request, timeout=2) as response:
        return json.load(response)


def _daemon_session_count(port: int) -> int:
    return len(_authed_get(port, "/api/sessions")["sessions"])


def _check_version_coverage(label: str, sessions, tested: tuple[int, int]) -> None:
    """issue #22: surface a source running a newer version than the checked-in
    fixtures were captured from -- fixtures don't update themselves."""
    for s in sessions:
        if s.version:
            parts = tuple(int(x) for x in s.version.split(".")[:2])
            if parts > tested:
                _warn(f"{label} {s.version} newer than tested {tested}",
                      "parsers are tolerant, but verify states look right")
            break


def _report_source_health(port: int) -> None:
    """issue #15: surface any background source that's currently failing --
    a source going dark otherwise just looks like a stale dashboard."""
    sources = _authed_get(port, "/api/health")["sources"]
    for name, h in sources.items():
        failing = h["last_error_ts"] is not None and (
            h["last_success"] is None or h["last_error_ts"] > h["last_success"])
        if failing:
            age = int(time.time() - h["last_error_ts"])
            _warn(f"{name} failing", f"{h['last_error_class']}, {h['error_count']}x, "
                                     f"last {age}s ago")


def run_doctor() -> int:
    ok = True

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
    _check_version_coverage("claude", claude_code.scan(), TESTED_CLAUDE)
    _check_version_coverage("codex", codex_src.scan(), TESTED_CODEX)

    print("daemon:")
    daemon_json = config.STATE_DIR / "daemon.json"
    if daemon_json.exists():
        try:
            info = json.loads(daemon_json.read_text())
            n = _daemon_session_count(info["port"])
            _check("daemon running", True, f"port {info['port']}, {n} sessions")
            _report_source_health(info["port"])
        except Exception:
            _warn("daemon state file present but daemon unreachable",
                  "stale daemon.json or crashed daemon")
    else:
        _warn("daemon not running", "start with `huginn serve`")

    return 0 if ok else 1
