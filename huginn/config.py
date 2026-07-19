"""Config load/save (~/.config/huginn/config.toml) and state paths."""
from __future__ import annotations

import copy
import os
import tomllib
from pathlib import Path
from typing import Any

CONFIG_DIR = Path.home() / ".config" / "huginn"
CONFIG_PATH = CONFIG_DIR / "config.toml"
STATE_DIR = Path.home() / ".local" / "state" / "huginn"
CACHE_DIR = STATE_DIR / "cache"

DEFAULTS: dict[str, dict[str, Any]] = {
    "server": {"host": "127.0.0.1", "port": 47100},
    "llm": {
        "enabled": True,
        "provider": "claude",              # claude | codex
        "blurb_model": "claude-haiku-4-5",
        "chat_model": "",                  # empty = provider default
        "blurb_debounce_s": 3.0,
        "blurb_max_per_min": 12,
        "blurb_timeout_s": 30.0,
    },
    # Compatibility fallback for Notification payloads that predate Claude
    # Code's structured notification_type field.
    "patterns": {
        "permission": ["permission", "approve", "authoriz", "allowed"],
        "waiting": ["waiting for", "your input", "idle"],
        # opt-in: append raw Notification message text (message only, no other
        # hook fields) to ~/.local/state/huginn/notifications.log -- issue #1,
        # for tuning the two pattern lists above against real traffic.
        "debug_log": False,
    },
    "ui": {"show_ended": True, "ended_ttl_s": 300},
    "claude": {"sweep_s": 10.0, "pending_tool_timeout_s": 20.0},
    "codex": {"poll_s": 5.0, "active_window_h": 24, "include_subagents": False},
    "claude_desktop": {"enabled": True, "poll_s": 15.0},
}


class Config:
    def __init__(self, data: dict[str, Any]):
        self.data = data

    def get(self, section: str, key: str) -> Any:
        return self.data.get(section, {}).get(key, DEFAULTS.get(section, {}).get(key))

    def section(self, section: str) -> dict[str, Any]:
        merged = dict(DEFAULTS.get(section, {}))
        merged.update(self.data.get(section, {}))
        return merged

    def update(self, section: str, key: str, value: Any) -> None:
        self.data.setdefault(section, {})[key] = value

    def to_dict(self) -> dict[str, Any]:
        return {s: self.section(s) for s in DEFAULTS}


def load() -> Config:
    data = copy.deepcopy(DEFAULTS)
    if CONFIG_PATH.exists():
        try:
            file_data = tomllib.loads(CONFIG_PATH.read_text())
            for section, values in file_data.items():
                if isinstance(values, dict):
                    data.setdefault(section, {}).update(values)
        except (tomllib.TOMLDecodeError, OSError):
            pass  # bad config falls back to defaults; doctor reports it
    return Config(data)


def _toml_value(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, str):
        return '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'
    if isinstance(v, list):
        return "[" + ", ".join(_toml_value(x) for x in v) + "]"
    raise TypeError(f"unsupported config value type: {type(v)}")


def save(cfg: Config) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    for section in DEFAULTS:
        lines.append(f"[{section}]")
        for key, value in cfg.section(section).items():
            lines.append(f"{key} = {_toml_value(value)}")
        lines.append("")
    tmp = CONFIG_PATH.with_suffix(".toml.tmp")
    tmp.write_text("\n".join(lines))
    os.replace(tmp, CONFIG_PATH)


def ensure_state_dirs() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)


TOKEN_PATH = STATE_DIR / "token"


def write_token() -> str:
    """Fresh per-daemon-start auth token; any open dashboard tab hard-reloads on 401."""
    import secrets
    ensure_state_dirs()
    token = secrets.token_urlsafe(32)
    tmp = TOKEN_PATH.with_suffix(".tmp")
    tmp.write_text(token)
    tmp.chmod(0o600)
    os.replace(tmp, TOKEN_PATH)
    return token
