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
    # Code's structured notification_type field: anything matching
    # "permission" is a permission prompt, anything else is treated as
    # needing input (there's no third Notification-hook outcome, so a
    # separate "waiting" pattern list would have nothing to distinguish --
    # issue #19 removed it rather than ship a config knob that can't affect
    # behavior).
    "patterns": {
        "permission": ["permission", "approve", "authoriz", "allowed"],
        # opt-in: append raw Notification message text (message only, no other
        # hook fields) to ~/.local/state/huginn/notifications.log -- issue #1,
        # for tuning the pattern list above against real traffic.
        "debug_log": False,
    },
    "ui": {
        "show_ended": True,
        "ended_ttl_s": 300,
        "idle_ttl_s": 300,
        "done_ttl_s": 300,
        "exec_done_ttl_s": 30,
    },
    "claude": {"sweep_s": 10.0, "pending_tool_timeout_s": 20.0},
    "codex": {"poll_s": 5.0, "active_window_h": 24, "include_subagents": False},
    "claude_desktop": {"enabled": True, "poll_s": 15.0},
}


# Numeric settings that must be > 0 -- a zero/negative poll interval or
# timeout doesn't fail loudly, it just wedges a background task forever.
_POSITIVE_NUMERIC_KEYS = {
    ("llm", "blurb_debounce_s"), ("llm", "blurb_max_per_min"), ("llm", "blurb_timeout_s"),
    ("ui", "ended_ttl_s"), ("ui", "idle_ttl_s"), ("ui", "done_ttl_s"),
    ("ui", "exec_done_ttl_s"), ("claude", "sweep_s"), ("claude", "pending_tool_timeout_s"),
    ("codex", "poll_s"), ("codex", "active_window_h"), ("claude_desktop", "poll_s"),
}
_ENUM_KEYS: dict[tuple[str, str], set[str]] = {
    ("llm", "provider"): {"claude", "codex"},
}


def validate_setting(section: str, key: str, value: Any) -> str | None:
    """Type/range/enum-check one (section, key, value) against DEFAULTS'
    shape. Returns an error message, or None if the value is acceptable --
    issue #18: PUT /api/settings must reject bad values before writing them
    to runtime config or disk, not let them fail later in a background task."""
    if section not in DEFAULTS or key not in DEFAULTS[section]:
        return f"unknown setting: {section}.{key}"
    default = DEFAULTS[section][key]

    enum = _ENUM_KEYS.get((section, key))
    if enum is not None:
        if value not in enum:
            return f"{section}.{key} must be one of {sorted(enum)}"
        return None
    if isinstance(default, bool):
        if not isinstance(value, bool):
            return f"{section}.{key} must be a boolean"
        return None
    if isinstance(default, int):   # after the bool check -- bool is an int subclass
        if isinstance(value, bool) or not isinstance(value, int):
            return f"{section}.{key} must be an integer"
        if section == "server" and key == "port" and not (1 <= value <= 65535):
            return f"{section}.{key} must be between 1 and 65535"
        if (section, key) in _POSITIVE_NUMERIC_KEYS and value <= 0:
            return f"{section}.{key} must be greater than 0"
        return None
    if isinstance(default, float):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return f"{section}.{key} must be a number"
        if (section, key) in _POSITIVE_NUMERIC_KEYS and value <= 0:
            return f"{section}.{key} must be greater than 0"
        return None
    if isinstance(default, str):
        if not isinstance(value, str):
            return f"{section}.{key} must be a string"
        return None
    if isinstance(default, list):
        if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
            return f"{section}.{key} must be a list of strings"
        return None
    return f"{section}.{key}: no validator for this setting"   # pragma: no cover


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


_TOML_ESCAPES = {"\\": "\\\\", '"': '\\"', "\b": "\\b", "\t": "\\t",
                 "\n": "\\n", "\f": "\\f", "\r": "\\r"}


def _toml_string(s: str) -> str:
    """A raw control character (bare newline, tab, ...) inside a TOML basic
    string is invalid syntax, not just ugly -- issue #18. Escape every
    control character, not just backslash/quote."""
    out = []
    for ch in s:
        if ch in _TOML_ESCAPES:
            out.append(_TOML_ESCAPES[ch])
        elif ord(ch) < 0x20 or ord(ch) == 0x7f:
            out.append(f"\\u{ord(ch):04x}")
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


def _toml_value(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, str):
        return _toml_string(v)
    if isinstance(v, list):
        return "[" + ", ".join(_toml_value(x) for x in v) + "]"
    raise TypeError(f"unsupported config value type: {type(v)}")


def secure_dir(path: Path) -> None:
    """mkdir + chmod 0700 explicitly -- mkdir's own mode= is masked by
    umask, which isn't good enough for directories holding prompts/tokens."""
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(0o700)


def save(cfg: Config) -> None:
    secure_dir(CONFIG_DIR)
    lines: list[str] = []
    for section in DEFAULTS:
        lines.append(f"[{section}]")
        # Only ever write keys DEFAULTS still knows about -- self-heals a
        # config file carrying a since-removed key forward (issue #19)
        # instead of preserving it indefinitely.
        for key in DEFAULTS[section]:
            lines.append(f"{key} = {_toml_value(cfg.get(section, key))}")
        lines.append("")
    tmp = CONFIG_PATH.with_suffix(".toml.tmp")
    tmp.write_text("\n".join(lines))
    tmp.chmod(0o600)
    os.replace(tmp, CONFIG_PATH)


def ensure_state_dirs() -> None:
    secure_dir(STATE_DIR)
    secure_dir(CACHE_DIR)


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
