"""Publishing and reading a raven descriptor in the shared state directory (#42).

A "raven" is a local process that advertises itself to a shared status menu bar
by dropping a small JSON descriptor into one well-known directory. The host lists
that directory, checks each descriptor is backed by a live process, and fetches a
menu from the port it names. This module is the parts of that both raven projects
implement identically:

* :func:`state_dir` / :func:`descriptor_path` -- **where**, which is the contract.
* :func:`publish_descriptor` -- an atomic, owner-only write.
* :func:`withdraw_descriptor` -- an ownership-checked removal.
* :func:`descriptor_is_live` -- the same liveness rule the host applies, so a
  ``doctor`` command can tell the user what the host will conclude.

**What the descriptor says is deliberately not here.** The two projects publish
genuinely different documents -- one is authenticated and offers actions, the
other is token-less and link-only -- and a shared "descriptor payload builder"
would either grow a parameter per field or force a shape neither wants. Each
project builds its own dict and hands it to :func:`publish_descriptor`. Likewise
absent: menu content and the HTTP surface that serves it.

The resolution rule in :func:`state_dir` is the one thing here that must match
the host and every other raven byte for byte. Get it wrong and the failure is
completely silent: a raven with no descriptor where the host is looking is
indistinguishable from a raven that was never installed -- an empty menu with
nothing on screen to explain it. That is the whole reason it is shared code
rather than a documented convention.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

#: Environment override for the shared descriptor directory, named by the raven
#: protocol rather than by either project. It is what lets a test harness -- or a
#: user who relocates state wholesale -- point every participant, host included,
#: at one alternate location.
STATE_DIR_ENV = "RAVENS_STATE_DIR"

#: How much slack to allow when cross-checking a recorded ``started`` against the
#: OS's own record of when a pid began. The host uses the same figure: a raven may
#: read its start time a moment after the process actually began, and a strict
#: comparison would report every live raven as a recycled pid.
STARTED_SLACK = 2.0


def state_dir() -> Path:
    """Return the *shared* raven descriptor directory.

    Resolution order, which every participant must implement identically:

    1. ``$RAVENS_STATE_DIR`` when set and non-empty.
    2. Windows: ``%LOCALAPPDATA%\\Ravens``, falling back to
       ``~\\AppData\\Local\\Ravens``.
    3. POSIX: ``$XDG_STATE_HOME/ravens``, falling back to
       ``~/.local/state/ravens``.

    **This is not a consumer's own state directory and must never be replaced by
    one.** That is the mistake worth naming: a project's own state dir resolves
    to ``.../huginn`` or ``.../muninn``, and a descriptor written there is a
    descriptor the host never looks at.

    Note this honours ``XDG_STATE_HOME`` even where a consumer's own state
    directory does not. The asymmetry is deliberate: a project's own state
    location is its business and moving it is a compatibility change of its own,
    whereas this directory is *shared*, so replicating a project's quirk here
    would publish where the host is not looking on any machine that sets
    ``XDG_STATE_HOME``.

    Read every call rather than cached at import, so a test (or a user) can set
    the override and have it take effect.
    """
    override = os.environ.get(STATE_DIR_ENV, "").strip()
    if override:
        return Path(override).expanduser()
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA", "").strip()
        base = Path(local) if local else Path.home() / "AppData" / "Local"
        return base / "Ravens"
    xdg = os.environ.get("XDG_STATE_HOME", "").strip()
    base = Path(xdg) if xdg else Path.home() / ".local" / "state"
    return base / "ravens"


def descriptor_path(name: str, *, directory: Path | None = None) -> Path:
    """Where the raven called ``name`` publishes, e.g. ``.../ravens/huginn.json``."""
    base = state_dir() if directory is None else Path(directory)
    return base / f"{name}.json"


def publish_descriptor(name: str, payload: dict[str, Any], *,
                       directory: Path | None = None) -> Path:
    """Write ``payload`` as ``{name}.json`` atomically and owner-only. Returns its path.

    Call this **after** the port is bound, never before: a descriptor naming a
    port that is not yet listening makes the host report a healthy raven as
    unreachable during startup.

    Atomic because the host may read at any moment and must never see a partial
    file. The temp file is staged in the same directory so ``os.replace`` cannot
    cross a filesystem boundary and silently fall back to a non-atomic copy, and
    its mode is set *before* the replace -- creating the final file first and
    chmodding after leaves a window in which it is world-readable.

    0600 even though a descriptor need hold no secret: it names a loopback port
    and possibly a token path that another process reads and *acts on*, so
    integrity matters where confidentiality may not.

    The directory is created 0700 and, if it already exists, **left exactly as
    it is**. It is shared with other ravens owned by the same user, and silently
    retightening another project's directory is not ours to do. Note "shared with
    other ravens", not with other users: one user's raven has no business reading
    another user's descriptors.

    Raises ``OSError`` if the directory or file cannot be written. Callers
    generally treat that as "no menu row" rather than fatal -- discovery is worth
    less than whatever the process is actually for.
    """
    base = state_dir() if directory is None else Path(directory)
    try:
        base.mkdir(parents=True, mode=0o700)
    except FileExistsError:
        pass
    target = base / f"{name}.json"
    fd, tmp_name = tempfile.mkstemp(prefix=f".{name}.", dir=str(base))
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        _restrict(tmp, 0o600)
        os.replace(tmp, target)
    finally:
        tmp.unlink(missing_ok=True)
    return target


def withdraw_descriptor(name: str, *, pid: int | None = None,
                        directory: Path | None = None) -> bool:
    """Remove our descriptor if it is still ours. Returns whether it was removed.

    Ownership-checked, and that check is the point: a second instance that lost a
    port race, or a replacement that has already republished, must not have its
    descriptor deleted by our exit. A descriptor whose recorded ``pid`` is not
    ``pid`` is left alone.

    Best-effort and deliberately not more than that. A ``SIGKILL`` skips this
    entirely and the host copes, because it verifies the recorded pid before
    trusting the file -- so a stale descriptor renders with a visible reason
    rather than as a phantom raven. Adding machinery to guarantee removal would
    buy nothing the liveness check does not already provide, and would run in the
    shutdown path where it can only make things worse.

    A corrupt or unreadable descriptor is left in place rather than guessed at:
    the host refuses an unparseable descriptor with a visible reason anyway, and
    deleting a file we cannot prove is ours is the thing this function exists not
    to do.
    """
    process_id = os.getpid() if pid is None else pid
    path = descriptor_path(name, directory=directory)
    try:
        if json.loads(path.read_text(encoding="utf-8")).get("pid") == process_id:
            path.unlink()
            return True
    except (OSError, ValueError, AttributeError):
        pass
    return False


def read_descriptor(name: str, *, directory: Path | None = None) -> dict[str, Any] | None:
    """Return a published descriptor, or None if it is absent or unreadable.

    None conflates "no such raven" with "malformed descriptor" on purpose: a
    caller that needs to tell them apart should stat the path itself, and every
    caller that does not would otherwise have to catch two exception families to
    ask one question.
    """
    path = descriptor_path(name, directory=directory)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def descriptor_is_live(payload: dict[str, Any] | None, *,
                       pid_alive: Any, process_start_time: Any = None,
                       slack: float = STARTED_SLACK) -> bool:
    """Apply the host's own liveness rule to a descriptor.

    The rule, in the order the host applies it: the recorded ``pid`` must be an
    int naming a running process, and *if* a ``started`` timestamp is recorded and
    the OS can say when that pid actually began, the two must agree within
    ``slack`` seconds. Both halves matter -- pids are recycled, so a live process
    at the recorded pid is not on its own evidence that it is the raven that wrote
    the file.

    ``pid_alive`` and ``process_start_time`` are injected callables rather than
    implemented here, and that is not indirection for its own sake: reading a
    process start time needs ``libproc`` on macOS, ``/proc`` on Linux, and
    ``GetProcessTimes`` on Windows, and corvidae is stdlib-only with no business
    owning a process-inspection layer. Both consumers already have one.

    ``process_start_time`` may be omitted, or may return None for a platform that
    cannot answer. Then the pid check stands alone -- a missing cross-check must
    not turn a live raven into a dead one, which would be the worse failure of the
    two: the user is told nothing is running while it is.

    A negative or zero ``started`` is treated as absent. That is how a producer
    that could not read a real start time records "unknown", and comparing
    against it would fail for every live process rather than only recycled ones.
    """
    if not isinstance(payload, dict):
        return False
    pid = payload.get("pid")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    if not pid_alive(pid):
        return False
    started = payload.get("started")
    if not isinstance(started, (int, float)) or isinstance(started, bool) or started <= 0:
        return True
    if process_start_time is None:
        return True
    try:
        actual = process_start_time(pid)
    except Exception:
        # An inspection failure is not evidence of death. The pid check already
        # passed, and reporting a running raven as gone is the worse error.
        return True
    if not actual:
        return True
    return abs(float(actual) - float(started)) <= slack


def _restrict(path: Path, mode: int) -> None:
    """Best-effort owner-only mode. A no-op on Windows, which uses ACLs.

    Windows is not silently ignored so much as deliberately left alone: NTFS does
    not honour mode bits, and corvidae has no pywin32 dependency to set a DACL
    with. A descriptor names a port, so on Windows this file is about as sensitive
    as the fact that the process is running, which is already in the task list.
    """
    if sys.platform == "win32":
        return
    try:
        path.chmod(mode)
    except OSError:
        pass


__all__ = [
    "STARTED_SLACK",
    "STATE_DIR_ENV",
    "descriptor_is_live",
    "descriptor_path",
    "publish_descriptor",
    "read_descriptor",
    "state_dir",
    "withdraw_descriptor",
]
