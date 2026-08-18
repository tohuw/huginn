"""Config load/save (~/.config/huginn/config.toml) and state paths."""
from __future__ import annotations

import copy
import os
import re
import sys
import tomllib
from pathlib import Path
from typing import Any

if sys.platform == "win32":
    CONFIG_DIR = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / "Huginn"
    STATE_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "Huginn"
else:
    CONFIG_DIR = Path.home() / ".config" / "huginn"
    STATE_DIR = Path.home() / ".local" / "state" / "huginn"
CONFIG_PATH = CONFIG_DIR / "config.toml"
CACHE_DIR = STATE_DIR / "cache"

DEFAULTS: dict[str, dict[str, Any]] = {
    "server": {"host": "127.0.0.1", "port": 47100},
    "llm": {
        "enabled": True,
        "provider": "claude",              # claude | codex
        "blurb_model": "haiku",             # provider may translate this intent
        "chat_model": "",                  # empty = provider default
        "blurb_debounce_s": 3.0,
        "blurb_max_per_min": 6,
        "blurb_max_per_day": 200,
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
        "show_desktop": True,
        "view": "cards",                 # cards | list
        "sort": "state",                 # state | alpha | newest | oldest
        "live": True,                     # false holds the dashboard card snapshot
        "chat_open": True,
        "chat_span": "vertical",        # vertical | horizontal
        "ended_ttl_s": 300,
        "idle_ttl_s": 300,
        "done_ttl_s": 300,
        "exec_done_ttl_s": 30,
        # Session.group keys currently collapsed to their one-line toggle --
        # see docs/plugins.md's "Dashboard session groups" section.
        "hidden_groups": [],
        # Session.group keys whose cards are secondarily ordered by the
        # plugin-contributed group_sort_key. The chosen primary sort still
        # applies within each secondary value.
        "group_sorts": [],
    },
    "claude": {"sweep_s": 10.0, "pending_tool_timeout_s": 20.0},
    "codex": {"poll_s": 5.0, "active_window_h": 24, "include_subagents": False},
    "claude_desktop": {"enabled": True, "poll_s": 15.0},
    "chatgpt_desktop": {"enabled": True, "poll_s": 15.0},
    # Empty distros means the user's default WSL distribution.
    "wsl": {"enabled": sys.platform == "win32", "poll_s": 5.0, "distros": []},
    # How far Huginn's derived view may trail the newest source artifact before
    # `huginn doctor` warns -- issue #39. Every benign gap is seconds to
    # minutes: codex polls every 5s, the claude sweep runs every 10s, codex's
    # active-rollout window is 240s, and roster TTLs are 300s. One hour is more
    # than an order of magnitude above all of them, so it cannot fire during
    # normal operation, yet it is far below both the 7-day silent staleness
    # that motivated the check and Claude Code's 30-day cleanupPeriodDays
    # sweep -- the deadline past which unprocessed transcripts are deleted
    # rather than merely late.
    "doctor": {"max_lag_s": 3600.0},
}


# Numeric settings that must be > 0 -- a zero/negative poll interval or
# timeout doesn't fail loudly, it just wedges a background task forever.
_POSITIVE_NUMERIC_KEYS = {
    ("llm", "blurb_debounce_s"), ("llm", "blurb_max_per_min"),
    ("llm", "blurb_max_per_day"), ("llm", "blurb_timeout_s"),
    ("ui", "ended_ttl_s"), ("ui", "idle_ttl_s"), ("ui", "done_ttl_s"),
    ("ui", "exec_done_ttl_s"), ("claude", "sweep_s"), ("claude", "pending_tool_timeout_s"),
    ("codex", "poll_s"), ("codex", "active_window_h"), ("claude_desktop", "poll_s"),
    ("chatgpt_desktop", "poll_s"),
    ("wsl", "poll_s"),
    ("doctor", "max_lag_s"),
}
_ENUM_KEYS: dict[tuple[str, str], set[str]] = {
    ("ui", "view"): {"cards", "list"},
    ("ui", "sort"): {"state", "alpha", "newest", "oldest"},
    ("ui", "chat_span"): {"vertical", "horizontal"},
}
_UI_GROUP_KEY_RE = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$")
_MAX_UI_GROUP_KEYS = 100
_MAX_UI_GROUP_KEY_CHARS = 80


def validate_setting(section: str, key: str, value: Any) -> str | None:
    """Type/range/enum-check one (section, key, value) against DEFAULTS'
    shape. Returns an error message, or None if the value is acceptable --
    issue #18: PUT /api/settings must reject bad values before writing them
    to runtime config or disk, not let them fail later in a background task."""
    if section not in DEFAULTS or key not in DEFAULTS[section]:
        return f"unknown setting: {section}.{key}"
    default = DEFAULTS[section][key]

    if (section, key) == ("llm", "provider"):
        if not isinstance(value, str):
            return "llm.provider must be a string"
        from .llm.providers import all_providers
        if value not in all_providers():
            return f"llm.provider must name an installed provider: {value}"
        # issue #41: config may narrow the allowed set but never widen it, so a
        # provider an installed policy forbids is rejected here rather than
        # written to disk and refused later at the chokepoint. With no policy
        # installed this is always None -- the default stays permissive.
        from .policy import provider_refusal
        return provider_refusal(value)

    if (section, key) in (("llm", "chat_model"), ("llm", "blurb_model")):
        if not isinstance(value, str):
            return f"{section}.{key} must be a string"
        from .policy import DEFAULT_POLICY, refusal, resolve
        if resolve() == (DEFAULT_POLICY,):
            return None   # unrestricted: don't re-read config.toml to learn nothing
        # Validated against the provider *currently* configured: settings are
        # applied per-key, and a batch that changes both is validated before
        # anything mutates, so this deliberately checks the provider in effect
        # rather than one being set in the same request. The chokepoint at the
        # call site is what guarantees the pair is legal at use time.
        return refusal(value, load().get("llm", "provider"))

    enum = _ENUM_KEYS.get((section, key))
    if enum is not None:
        if value not in enum:
            return f"{section}.{key} must be one of {sorted(enum)}"
        return None
    if (section, key) == ("ui", "group_sorts"):
        if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
            return "ui.group_sorts must be a list of strings"
        if len(value) > _MAX_UI_GROUP_KEYS:
            return f"ui.group_sorts is limited to {_MAX_UI_GROUP_KEYS} groups"
        if any(len(x) > _MAX_UI_GROUP_KEY_CHARS or not _UI_GROUP_KEY_RE.fullmatch(x)
               for x in value):
            return "ui.group_sorts contains an invalid group key"
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
            file_data = tomllib.loads(CONFIG_PATH.read_text(encoding="utf-8"))
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
    tmp.write_text("\n".join(lines), encoding="utf-8")
    tmp.chmod(0o600)
    os.replace(tmp, CONFIG_PATH)


def ensure_state_dirs() -> None:
    secure_dir(STATE_DIR)
    secure_dir(CACHE_DIR)


TOKEN_PATH = STATE_DIR / "token"
REFRESH_TOKEN_PATH = STATE_DIR / "refresh-token"


def write_token() -> str:
    """Fresh per-daemon-start API token."""
    import secrets
    import tempfile
    ensure_state_dirs()
    token = secrets.token_urlsafe(32)
    fd, tmp_name = tempfile.mkstemp(prefix="token.", dir=STATE_DIR)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(token)
        tmp.chmod(0o600)
        os.replace(tmp, TOKEN_PATH)
    finally:
        tmp.unlink(missing_ok=True)
    return token


def get_or_create_refresh_token() -> str:
    """Persistent browser credential used only to recover after API-token rotation."""
    import secrets
    ensure_state_dirs()
    try:
        token = REFRESH_TOKEN_PATH.read_text(encoding="utf-8").strip()
        if token:
            return token
    except OSError:
        pass
    token = secrets.token_urlsafe(32)
    tmp = REFRESH_TOKEN_PATH.with_suffix(".tmp")
    tmp.write_text(token, encoding="utf-8")
    tmp.chmod(0o600)
    os.replace(tmp, REFRESH_TOKEN_PATH)
    return token
