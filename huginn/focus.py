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


def focus_session(s: Session) -> dict[str, Any]:
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
            if s.cwd:
                editor = _platform.focus_vscode(s.cwd)
                if editor.ok:
                    return _result(True, editor.target)
            return _result(False, detail=terminal.detail or "Codex CLI terminal not found")
        return _result(_open_app("ChatGPT"), "ChatGPT")

    if s.source == "claude-desktop":
        return _result(_open_app("Claude"), "Claude")

    if s.source == "chatgpt-desktop":
        return _result(_open_app("ChatGPT"), "ChatGPT")

    if s.entrypoint == "cli" and s.pid:
        tty = s.tty or find_tty(s.pid)
        if tty:
            s.tty = tty
        terminal = _platform.focus_terminal(s.pid, tty)
        if terminal.ok:
            return _result(True, terminal.target, detail=terminal.detail, **({"tty": tty} if tty else {}))

    if s.cwd:
        editor = _platform.focus_vscode(s.cwd)
        if editor.ok:
            return _result(True, editor.target)
    return _result(False, detail="no focus target found")


__all__ = ["find_tty", "focus_session"]
