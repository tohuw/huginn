"""huginn doctor: verify environment, data sources, hooks, and daemon health."""
from __future__ import annotations

import json
import os
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


def _daemon_sessions(port: int) -> list[dict]:
    return _authed_get(port, "/api/sessions")["sessions"]


def _daemon_session_count(port: int) -> int:
    return len(_daemon_sessions(port))


def _snapshot_sessions() -> list[dict]:
    """The derived roster the daemon last persisted.

    Lag matters most when the daemon is *not* running -- a stopped daemon is
    precisely how a view goes days stale -- so the report falls back to the
    on-disk snapshot rather than skipping the check (issue #39).
    """
    try:
        data = json.loads((config.STATE_DIR / "sessions.json").read_text())
    except (OSError, json.JSONDecodeError):
        return []
    sessions = data.get("sessions")
    return list(sessions.values()) if isinstance(sessions, dict) else []


def _report_data_lag(sessions: list[dict], cfg: config.Config) -> None:
    """issue #39: report newest source artifact vs. newest derived timestamp
    per source. Every way the pipeline can fall behind -- a wedged watcher, a
    poller blocked on a sandboxed read, a parser that stopped recognizing an
    upgraded source's artifacts -- otherwise looks identical to a quiet
    dashboard, and Claude Code's cleanupPeriodDays sweep eventually deletes
    what was never processed."""
    from . import lag
    from .plugins import get_registry
    probes: dict[str, lag.ArtifactProbe | None] = dict(lag.builtin_probes(cfg))
    probes.update(lag.plugin_probes(get_registry()))
    max_lag_s = cfg.get("doctor", "max_lag_s")
    now = time.time()
    for entry in lag.collect(lag.newest_processed(sessions), probes):
        detail = lag.describe(entry, now)
        if entry.stale(max_lag_s):
            _warn(f"{entry.source} data lag", f"{detail}, over {int(max_lag_s)}s threshold")
        else:
            _check(f"{entry.source} data lag", True, detail)


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
    cfg = config.load()

    print("binaries:")
    from .llm.providers import claude_binary, codex_binary
    cb = claude_binary()
    ok &= _check("claude CLI", bool(cb), cb or "not found via `whence -p claude`")
    codex_path = codex_binary()
    codex_ok = bool(codex_path)
    _check("codex CLI", codex_ok, codex_path or "codex executable not found")
    if codex_ok:
        auth = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "auth.json"
        _check("codex auth", auth.exists(), str(auth))

    print("plugins:")
    from .plugins import get_registry
    registry = get_registry()
    for plugin in registry.plugins:
        capabilities = len(plugin.providers) + len(plugin.sources)
        _check(plugin.name, True, f"{plugin.version}, {capabilities} capabilities")
    if not registry.plugins:
        _check("installed plugins", True, "none")
    for error in registry.errors:
        ok &= _check(
            error.entry_point,
            False,
            f"{error.error_class}: {error.detail}",
        )

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
    roster: list[dict] | None = None
    daemon_json = config.STATE_DIR / "daemon.json"
    if daemon_json.exists():
        try:
            info = json.loads(daemon_json.read_text())
            roster = _daemon_sessions(info["port"])
            _check("daemon running", True, f"port {info['port']}, {len(roster)} sessions")
            _report_source_health(info["port"])
        except Exception:
            _warn("daemon state file present but daemon unreachable",
                  "stale daemon.json or crashed daemon")
    else:
        _warn("daemon not running", "start with `huginn serve`")

    print("data lag:")
    if roster is None:
        roster = _snapshot_sessions()
        _warn("live roster unavailable", "measuring lag against the last snapshot")
    _report_data_lag(roster, cfg)

    return 0 if ok else 1
