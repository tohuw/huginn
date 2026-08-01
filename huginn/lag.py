"""Data-lag probes: how far Huginn's derived view trails its sources -- issue #39.

Huginn derives its entire roster from files other tools write: Claude Code
status files and transcripts, Codex rollout JSONL.  Nothing in that pipeline
reported *how stale* the derived view was, and every way it can fall behind --
a wedged watcher, a poller blocked on a sandboxed read, a parser that quietly
stopped recognizing a source's artifacts -- looks exactly like "a quiet
dashboard".  A sibling tool built on these same directories sat 7 days stale
with 148 unprocessed transcripts while reporting success the whole time, and
Claude Code's own ``cleanupPeriodDays`` sweep (30 days by default) deletes
anything that was never processed in the meantime.  Staleness has to be
visible, not discovered.

Each source contributes two independent halves:

``newest_artifact``
    Newest mtime among the files that source would consume *right now* --
    restricted to the sessions it still considers live.  Leftover status files
    for dead PIDs are correctly ignored by the source, so counting them would
    report lag for work that was never Huginn's to do.

``newest_processed``
    Newest timestamp the derived roster holds for that source.

Both halves are optional and an absent half yields no lag rather than a
warning: an unknown lag is not evidence of a large one.  In particular a live
artifact with nothing derived from it is reported but not warned about,
because the daemon deliberately hides sessions that idled past
``ui.idle_ttl_s`` (a VS Code Claude backend parks for days) -- an empty roster
beside a parked artifact is correct behaviour, and the failure modes that
really do leave a source with nothing are already caught by doctor's
daemon-reachability and per-source health checks.
"""
from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from . import config

LOG = logging.getLogger("huginn.lag")

# Newest artifact mtime for one source, or None when it cannot be determined.
ArtifactProbe = Callable[[], float | None]

# Widest window a probe may report, as a Unix timestamp. A probe returns an
# mtime, so anything outside roughly 1970..2200 is a bug or a hostile value, not
# a measurement -- and issue #41 M3 showed what those do: 1e300 rendered a
# ~300-digit integer into doctor's report, and -inf made stale() return False,
# silently suppressing the staleness issue #39 exists to surface.
MIN_TIMESTAMP = 0.0
MAX_TIMESTAMP = 7_258_118_400.0   # 2200-01-01T00:00:00Z


def _sane_timestamp(value: Any) -> float | None:
    """A finite, in-window float, or None -- ``None`` reads as "unknown".

    ``isinstance(x, (int, float))`` alone accepted ``nan``/``inf``/``-inf`` and
    absurd magnitudes (issue #41 M3). ``nan`` crashed doctor with ``ValueError``
    and ``inf`` with ``OverflowError`` -- doctor is the tool you run *because*
    something is already wrong, so it must not be the thing that breaks -- while
    ``-inf`` was worse than a crash: it reported ``stale=False``.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number) or not MIN_TIMESTAMP <= number <= MAX_TIMESTAMP:
        return None
    return number

# Sources whose derived timestamp *is* an artifact mtime by construction --
# the desktop tiles and the WSL bridge report the same number they read -- have
# no gap to measure and are deliberately left unprobed.
_SELF_TIMED_SOURCES = ("claude-desktop", "chatgpt-desktop")


@dataclass(frozen=True)
class SourceLag:
    """One source's artifact/derived timestamp pair and the gap between them."""

    source: str
    newest_artifact: float | None = None
    newest_processed: float | None = None
    detail: str | None = None

    def lag_s(self) -> float | None:
        if self.newest_artifact is None or self.newest_processed is None:
            return None
        # A roster ahead of file mtimes is normal, not negative lag: hook
        # events land before the transcript flush that follows them.
        return max(0.0, self.newest_artifact - self.newest_processed)

    def stale(self, max_lag_s: float) -> bool:
        lag = self.lag_s()
        return lag is not None and lag > max_lag_s


def newest_mtime(paths: Iterable[Path]) -> float | None:
    """Newest mtime across paths, skipping any that vanished mid-scan."""
    newest: float | None = None
    for path in paths:
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if newest is None or mtime > newest:
            newest = mtime
    return newest


def _claude_artifact_mtime() -> float | None:
    """Newest write among live Claude Code artifacts.

    The status file is named after the owning PID (the daemon's watcher keys
    off the same convention), and its session's transcript is the file that
    actually carries turn-by-turn activity, so both count.
    """
    from .sources import claude_code

    paths: list[Path] = []
    for session in claude_code.scan():
        paths.append(claude_code.SESSIONS_DIR / f"{session.pid}.json")
        if session.transcript_path:
            paths.append(Path(session.transcript_path))
    return newest_mtime(paths)


def _codex_artifact_mtime(cfg: config.Config) -> float | None:
    """Newest rollout write among the Codex threads still on the live roster."""
    from .sources import codex

    return newest_mtime(
        Path(session.transcript_path)
        for session in codex.scan(cfg)
        if session.transcript_path
    )


def builtin_probes(cfg: config.Config) -> dict[str, ArtifactProbe]:
    return {
        "claude": _claude_artifact_mtime,
        "codex": lambda: _codex_artifact_mtime(cfg),
    }


def _guarded(probe: Any) -> ArtifactProbe:
    """Wrap a plugin-supplied probe so a broken one degrades to 'unknown'.

    Plugins are trusted code, but doctor is the tool you run *because*
    something is already wrong -- one plugin raising here must not take the
    rest of the report with it.
    """
    def call() -> float | None:
        try:
            value = probe()
        # BaseException, not Exception -- issue #41 M2: a probe raising
        # SystemExit took down the whole doctor run, violating this function's
        # own stated contract that one broken probe must not take the rest of
        # the report with it.
        except BaseException:
            LOG.debug("a lag probe raised; reporting its lag as unknown", exc_info=True)
            return None
        sane = _sane_timestamp(value)
        if sane is None and value is not None:
            LOG.debug("a lag probe returned %r, which is not a usable timestamp", value)
        return sane

    return call


def plugin_probes(registry: Any) -> dict[str, ArtifactProbe | None]:
    """Artifact probes for installed plugin sources, keyed by source name.

    A plugin source opts in by exposing ``artifact_mtime() -> float | None``.
    The value is None for sources that don't, which reports honestly rather
    than assuming a plugin watches a filesystem at all.
    """
    probes: dict[str, ArtifactProbe | None] = {}
    for _plugin, source in registry.sources():
        probe = getattr(source, "artifact_mtime", None)
        probes[source.name] = _guarded(probe) if callable(probe) else None
    return probes


def newest_processed(sessions: Iterable[Mapping[str, Any]]) -> dict[str, float]:
    """Newest derived ``last_activity`` per source name in a roster payload."""
    result: dict[str, float] = {}
    for session in sessions:
        if not isinstance(session, Mapping):
            continue
        # WSL sessions reuse the claude/codex source names but describe another
        # filesystem's artifacts; folding them in would mask host-side lag.
        if str(session.get("key") or "").startswith("wsl:"):
            continue
        source = session.get("source")
        if not isinstance(source, str):
            continue
        # Same finite/in-window check the probes get (issue #41 M3): a restored
        # snapshot is a file on disk, and an inf here would break doctor's
        # arithmetic just as surely as one from a plugin probe.
        ts = _sane_timestamp(session.get("last_activity"))
        if ts is None or ts <= 0:
            continue
        if ts > result.get(source, 0.0):
            result[source] = ts
    return result


def collect(
    processed: Mapping[str, float],
    probes: Mapping[str, ArtifactProbe | None],
) -> list[SourceLag]:
    """Pair every known source's artifact and derived timestamps.

    Sources are reported even when only one half is available, so a source
    that has gone entirely dark still gets a line instead of disappearing.
    """
    names = sorted(set(probes) | {n for n in processed if n not in _SELF_TIMED_SOURCES})
    entries: list[SourceLag] = []
    for name in names:
        probe = probes.get(name)
        artifact = probe() if probe is not None else None
        derived = processed.get(name)
        if name not in probes:
            # On the roster but contributed by nothing installed -- a restored
            # snapshot outliving the plugin that produced it.
            detail = "source is not installed"
        elif probe is None:
            detail = "source does not report artifact times"
        elif artifact is None:
            detail = "no live artifacts"
        elif derived is None:
            detail = "live artifacts, nothing derived yet"
        else:
            detail = None
        entries.append(SourceLag(name, artifact, derived, detail))
    return entries


def describe(entry: SourceLag, now: float | None = None) -> str:
    """Human-readable one-liner for doctor's report."""
    if entry.detail is not None:
        return entry.detail
    now = now or time.time()
    lag = entry.lag_s() or 0.0
    return (f"newest artifact {int(now - (entry.newest_artifact or now))}s ago, "
            f"derived {int(now - (entry.newest_processed or now))}s ago, "
            f"lag {int(lag)}s")


__all__ = [
    "ArtifactProbe",
    "SourceLag",
    "builtin_probes",
    "collect",
    "describe",
    "newest_mtime",
    "newest_processed",
    "plugin_probes",
]
