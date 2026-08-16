"""Portable, best-effort hook forwarder used by Claude Code and Codex."""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

from .. import config


def _terminal_identity() -> dict[str, str] | None:
    """What this session's terminal says about where it is, or None.

    Only WezTerm answers today, and it answers completely: ``WEZTERM_PANE`` is
    the pane's own id, ``WEZTERM_UNIX_SOCKET`` is the control socket of the GUI
    hosting it, and ``WEZTERM_EXECUTABLE`` is the binary that speaks to it. All
    three are exported into every pane, so nothing has to be discovered or
    guessed later -- which matters because the daemon runs outside any pane and
    could not find the socket on its own.

    Windows Terminal is the reason this exists and the reason it is empty
    there: it exports ``WT_SESSION``, a per-pane GUID, so a process *can* learn
    which pane it is in -- but Windows Terminal offers no way to focus a pane by
    that id. Identity without addressing, so there is nothing to record.
    """
    pane = (os.environ.get("WEZTERM_PANE") or "").strip()
    if not pane:
        return None
    identity = {"kind": "wezterm", "pane": pane}
    for key, name in (("WEZTERM_UNIX_SOCKET", "socket"),
                      ("WEZTERM_EXECUTABLE", "executable")):
        value = (os.environ.get(key) or "").strip()
        if value:
            identity[name] = value
    return identity


def main() -> int:
    # Hooks must never disrupt the coding-agent process, including when Huginn
    # is stopped or its state files are temporarily unavailable.
    try:
        source, event = sys.argv[1:3]
        port_path = config.STATE_DIR / "port"
        port = port_path.read_text(encoding="utf-8").strip() if port_path.exists() else "47100"
        token_path = config.TOKEN_PATH
        token = token_path.read_text(encoding="utf-8").strip() if token_path.exists() else ""
        # A windowless build may be handed no stdin at all, in which case
        # sys.stdin is None rather than an empty stream. An empty payload is
        # still a hook worth forwarding, so this degrades to "{}" instead of
        # raising -- the one thing a hook is never allowed to do.
        stream = getattr(sys.stdin, "buffer", None)
        payload = (stream.read() if stream is not None else b"") or b"{}"
        body = json.loads(payload)
        # This process is the only part of Huginn that runs *inside* the
        # session's terminal, so it is the only part that can see what the
        # terminal tells its children. A pid cannot locate a tab; an id the
        # terminal issued can. Attached here rather than discovered later
        # because the environment is gone the moment this process exits.
        terminal = _terminal_identity()
        if terminal and isinstance(body, dict):
            body["huginn_terminal"] = terminal
            payload = json.dumps(body).encode("utf-8")
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/hook/{source}/{event}",
            data=payload,
            headers={"Content-Type": "application/json", "X-Huginn-Token": token},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=1):
            pass
    except (IndexError, ValueError, OSError, AttributeError,
            json.JSONDecodeError, urllib.error.URLError):
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
