"""Append-only, idempotent hook installation for Claude Code and Codex.

Blast-radius rules: back up before writing, never touch entries we didn't add
(matched by 'huginn-hook' in the command), write via temp file + os.replace.
"""
from __future__ import annotations

import json
import os
import shutil
import stat
import time
from pathlib import Path

CLAUDE_SETTINGS = Path.home() / ".claude" / "settings.json"
CODEX_HOOKS = Path.home() / ".codex" / "hooks.json"
HOOK_BIN = Path.home() / ".local" / "bin" / "huginn-hook"
HOOK_SRC = Path(__file__).parent / "huginn-hook"

CLAUDE_EVENTS = ["SessionStart", "UserPromptSubmit", "Notification", "Stop", "SessionEnd"]
CODEX_EVENTS = ["SessionStart", "UserPromptSubmit", "Notification", "Stop", "SessionEnd"]


def _install_hook_bin() -> None:
    HOOK_BIN.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(HOOK_SRC, HOOK_BIN)
    HOOK_BIN.chmod(HOOK_BIN.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text())


def _write_json(path: Path, data: dict) -> None:
    backup = path.with_name(path.name + f".huginn-bak.{int(time.time())}")
    if path.exists():
        shutil.copyfile(path, backup)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    os.replace(tmp, path)


def _has_huginn(entries: list) -> bool:
    for entry in entries:
        for h in entry.get("hooks", []):
            if "huginn-hook" in h.get("command", ""):
                return True
    return False


def _merge_events(data: dict, events: list[str], source: str) -> int:
    hooks = data.setdefault("hooks", {})
    added = 0
    for event in events:
        entries = hooks.setdefault(event, [])
        if _has_huginn(entries):
            continue
        hook: dict = {"type": "command", "command": f"{HOOK_BIN} {source} {event}"}
        # Codex 0.145 skips hooks marked async ("not supported yet"); the
        # forwarder is fast enough (<0.3s worst case) to run sync there.
        if source == "claude":
            hook["async"] = True
        entries.append({"hooks": [hook]})
        added += 1
    return added


def _strip_events(data: dict) -> int:
    removed = 0
    hooks = data.get("hooks", {})
    for event in list(hooks):
        kept = []
        for entry in hooks[event]:
            inner = [h for h in entry.get("hooks", [])
                     if "huginn-hook" not in h.get("command", "")]
            removed += len(entry.get("hooks", [])) - len(inner)
            if inner:
                entry["hooks"] = inner
                kept.append(entry)
            elif not entry.get("hooks"):
                kept.append(entry)   # entry we don't understand; leave alone
        hooks[event] = kept
        if not hooks[event]:
            del hooks[event]
    return removed


def install() -> int:
    _install_hook_bin()
    total = 0
    for path, events, source in ((CLAUDE_SETTINGS, CLAUDE_EVENTS, "claude"),
                                 (CODEX_HOOKS, CODEX_EVENTS, "codex")):
        try:
            data = _load_json(path)
        except (json.JSONDecodeError, OSError) as e:
            print(f"skip {path}: unreadable ({e})")
            continue
        added = _merge_events(data, events, source)
        if added:
            _write_json(path, data)
        total += added
        print(f"{path}: {added} hook(s) added" + (" (backup written)" if added else ""))
    print(f"hook forwarder: {HOOK_BIN}")
    return 0 if total >= 0 else 1


def uninstall() -> int:
    for path in (CLAUDE_SETTINGS, CODEX_HOOKS):
        try:
            data = _load_json(path)
        except (json.JSONDecodeError, OSError):
            continue
        removed = _strip_events(data)
        if removed:
            _write_json(path, data)
        print(f"{path}: {removed} hook(s) removed")
    if HOOK_BIN.exists():
        HOOK_BIN.unlink()
        print(f"removed {HOOK_BIN}")
    return 0
