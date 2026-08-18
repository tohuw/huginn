"""Platform-neutral jump-to-session routing."""
from __future__ import annotations

from typing import Any

from .model import Session
from .platform import platform as _platform


def _tty_of(pid: int) -> str | None:
    return _platform.process_tty(pid)


def _children(pid: int) -> list[int]:
    return _platform.children(pid)


def _parent(pid: int) -> int | None:
    return _platform.parent(pid)


def find_tty(pid: int) -> str | None:
    """TTY of the process, its descendants (two levels), or parent shell."""
    tty = _tty_of(pid)
    if tty:
        return tty
    for child in _children(pid):
        tty = _tty_of(child)
        if tty:
            return tty
        for grandchild in _children(child):
            tty = _tty_of(grandchild)
            if tty:
                return tty
    parent = _parent(pid)
    return _tty_of(parent) if parent else None


def _focus_iterm_tty(tty: str) -> bool:
    """Compatibility wrapper retained for integrations and focused tests."""
    return _platform.focus_terminal(None, tty).ok


def _codex_tty_for_cwd(cwd: str) -> str | None:
    """Find a terminal-owned Codex CLI whose process cwd matches the card."""
    for pid in _platform.find_processes("codex"):
        if _platform.process_cwd(pid) == cwd:
            tty = _platform.process_tty(pid)
            if tty:
                return tty
    return None


def _open_app(name: str) -> bool:
    """Compatibility wrapper retained for integrations and focused tests."""
    return _platform.activate_app(name).ok


def _result(ok: bool, target: str | None = None, *, detail: str | None = None, **extra: Any) -> dict[str, Any]:
    value: dict[str, Any] = {"ok": ok}
    if target:
        value["target"] = target
    if detail:
        value["detail" if ok else "error"] = detail
    value.update(extra)
    return value


def _focus_recorded_pane(s: Session) -> dict[str, Any] | None:
    """Focus the exact tab a hook reported, or None to fall back to a window.

    None means "no exact route was available or it did not work", not failure:
    the caller still has the window-level path, and a raised terminal beats a
    refusal. The distinction matters because a recorded pane id goes stale in
    the ordinary course of things -- the tab is closed, or the terminal is.
    """
    focuser = getattr(_platform, "focus_pane", None)
    if focuser is None:
        return None

    # What the session reported about itself, then what the terminal can be
    # asked. The recorded pane is exact and comes first; discovery covers the
    # sessions no hook has reached yet, which after any daemon restart is all
    # of them until each one next does something. Without it, jump falls back
    # to raising a window for an idle tab -- the behaviour this replaces.
    candidates = []
    recorded = getattr(s, "terminal", None)
    if recorded:
        candidates.append(recorded)
    discover = getattr(_platform, "discover_pane", None)
    if discover is not None and s.cwd:
        found = discover(s.cwd)
        if found and found != recorded:
            candidates.append(found)

    for terminal in candidates:
        result = focuser(terminal)
        if result.ok:
            return _result(True, result.target, detail=result.detail)
    return None


def focus_session(s: Session) -> dict[str, Any]:
    handler_name = getattr(s, "focus_handler", None)
    if handler_name:
        from .plugins import get_registry
        handler = get_registry().focusers().get(handler_name)
        if handler is None:
            return _result(False, detail="session jump handler is unavailable")
        try:
            result = handler.focus(s)
        except Exception:
            return _result(False, detail="session jump handler failed")
        if isinstance(result, dict) and isinstance(result.get("ok"), bool):
            return result
        return _result(False, detail="session jump handler returned an invalid result")
    if s.source == "codex":
        # A WSL Codex row is still source="codex" for reducer semantics, but
        # it is never a ChatGPT desktop session. We do not yet have a stable
        # process-to-Windows-Terminal-tab mapping, so prefer its workspace and
        # then the best available terminal window.
        if (s.entrypoint or "").startswith("wsl:"):
            if s.cwd:
                editor = _platform.focus_vscode(s.cwd)
                if editor.ok:
                    return _result(True, editor.target)
            terminal = _platform.focus_terminal(None, None)
            if terminal.ok:
                return _result(True, terminal.target, detail=terminal.detail)
            return _result(False, detail=terminal.detail or "WSL terminal not found")
        if s.entrypoint in {"cli", "exec"}:
            # macOS can resolve an exact iTerm tab from cwd even though Codex
            # state rows generally have no pid. Windows falls back to the
            # owning/top-level Terminal window and reports that limitation.
            tty = _codex_tty_for_cwd(s.cwd) if s.cwd else None
            if tty and _focus_iterm_tty(tty):
                return _result(True, "iTerm2", tty=tty)
            terminal = _platform.focus_terminal(s.pid, tty)
            if terminal.ok:
                return _result(True, terminal.target, detail=terminal.detail)
            return _result(False, detail=terminal.detail or "Codex CLI terminal not found")
        return _result(_open_app("ChatGPT"), "ChatGPT")

    if s.source == "claude-desktop":
        return _result(_open_app("Claude"), "Claude")

    if s.source == "chatgpt-desktop":
        return _result(_open_app("ChatGPT"), "ChatGPT")

    if s.entrypoint == "cli":
        if not s.pid or not _platform.pid_alive(s.pid):
            return _result(False, detail="Claude CLI process is no longer running")
        # An exact tab first, when the terminal issued coordinates for one.
        # Everything below searches for a *window*, which is the best a pid can
        # do and is not good enough where one window holds many sessions.
        # Falls through on failure rather than giving up: a stale pane id means
        # the tab is gone, and raising the terminal is still better than
        # nothing.
        exact = _focus_recorded_pane(s)
        if exact is not None:
            return exact
        tty = s.tty or find_tty(s.pid)
        if tty:
            s.tty = tty
        terminal = _platform.focus_terminal(s.pid, tty)
        if terminal.ok:
            return _result(True, terminal.target, detail=terminal.detail, **({"tty": tty} if tty else {}))
        return _result(False, detail=terminal.detail or "Claude CLI terminal not found")

    if s.cwd:
        editor = _platform.focus_vscode(s.cwd)
        if editor.ok:
            return _result(True, editor.target)
    return _result(False, detail="no focus target found")


__all__ = ["find_tty", "focus_session"]
