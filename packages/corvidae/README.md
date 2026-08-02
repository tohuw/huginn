# corvidae

Shared internals for the raven agent-monitoring projects — [Huginn](https://github.com/tohuw/huginn)
and [Muninn](https://github.com/tohuw/muninn).

These pieces are genuinely reusable across both, and duplicating them meant
either a commit-pin relationship with Huginn or a second reimplementation of edge
cases that are expensive to get right once (issue
[tohuw/huginn#42](https://github.com/tohuw/huginn/issues/42)):

- **`Tail`** — a seek-from-end JSONL tailer for unbounded transcripts, with
  bounded backward widening for a record larger than the read window,
  incremental offsets, truncation/rotation recovery, and partial-line carry.
- **`redact_secrets`** — credential redaction for text leaving a transcript.
- **`Session` / `SessionState`** — one agreed session shape, so a plugin or a
  second consumer describes agent state the same way Huginn does.
- **`LoginAgentSpec` / `get_login_agent`** — start-at-login supervision, one
  backend per OS (launchd, systemd user units, the Windows Run key), with the
  injection and file-permission hardening intact. Originally
  [#39](https://github.com/tohuw/huginn/issues/39) ("No lag reporting for derived
  state, and background install is launchd-only"), built with sharing in mind; the
  hardening came out of a security review of the surface
  [#41](https://github.com/tohuw/huginn/issues/41) added, not from #41's own scope,
  which was the model-policy chokepoint.
- **`state_dir` / `publish_descriptor` / `withdraw_descriptor` /
  `descriptor_is_live`** — the raven descriptor protocol's shared directory and
  file discipline. The resolution rule in particular *must* match every other
  participant, and when it does not the failure is silent.
- **`sanitize_label`** — ANSI/C1/bidi stripping for untrusted text heading into a
  desktop menu row.

`corvidae` is stdlib-only, depends on nothing, and **never imports from
`huginn`**. The dependency direction is one-way: consumers depend on `corvidae`.

```python
from corvidae import ClaudeAnalyzer, Session, SessionState, Tail, redact_secrets
from corvidae import LoginAgentSpec, get_login_agent
from corvidae import publish_descriptor, sanitize_label, state_dir, withdraw_descriptor
```

## Stability contract

Everything in the list below is stable. Everything else in this package —
including every underscore-prefixed name, and any module-level name not listed
here — is an implementation detail with no promise attached.

**Promise: the names and signatures below keep working, unchanged in meaning,
for the remainder of the CalVer year in which they appear** (a `2026.*` release
is compatible with every other `2026.*` release). Within a year, changes are
additive only: new optional keyword arguments, new `Session` fields with
defaults, new enum members, new `activity()` dictionary keys. Nothing listed is
removed, renamed, or given a narrower type. A breaking change waits for the next
CalVer year and is called out in the release notes.

Explicit non-promises, so a consumer knows where the edges are:

- **`SessionState` and `activity()` dictionaries may gain members/keys within a
  year.** Do not write exhaustive `match` statements over `SessionState` without
  a default arm, and read `activity()` with `.get()`.
- **Redaction is best-effort.** `redact_secrets` recognises common credential
  *shapes*; a novel or unshaped secret can pass through. The function's
  *existence and signature* are stable, its exact pattern set is not — patterns
  are added freely (that is a fix, not a break). Never treat it as the sole
  reason it is safe to ship transcript text somewhere.
- **`LoginAgentSpec` may gain optional fields within a year.** Construct it with
  keywords past the five required ones; the required prefix
  (`name, label, argv, working_dir, log_path`) is what is pinned. Its
  *human-readable output* is not promised either: the exact wording each backend
  prints on install may change, so do not parse it — the exit code is the
  contract.
- **`sanitize_label`'s pattern set is outside the promise, and `MAX_LABEL` /
  `MAX_DETAIL` track the raven host protocol, not corvidae.** The function's
  existence, signature, and the guarantee that its output carries no ESC, C0/C1,
  DEL, bidi or zero-width character are stable; which additional characters it
  strips, and the two cap *values*, may change within a year. Read the constants
  rather than copying `120`/`80`.
- **`descriptor_is_live` implements the raven host's liveness rule, and that rule
  belongs to the host.** It is stable as an interface; if the host protocol
  changes what "live" means, this follows the host rather than the CalVer year.
  `STARTED_SLACK` is an implementation detail — pass `slack=` if you need a
  specific value.
- **`publish_descriptor` does not validate the payload.** What a descriptor
  *says* is deliberately each project's own business (Huginn's carries a token
  path, Muninn's does not), so this writes whatever dict it is given.

### `corvidae.transcript`

```python
class Tail:
    def __init__(self, path: str) -> None: ...
    offset: int                                  # bytes consumed so far
    def attach(self) -> list[dict]: ...          # seed from the last ATTACH_WINDOW bytes
    def read_new(self) -> list[dict]: ...        # one bounded incremental read
    def read_available(self) -> Iterator[list[dict]]: ...   # drain to current EOF

ATTACH_WINDOW: int    # 64 KiB   — initial seek-from-end window
MAX_READ: int         # 256 KiB  — cap on a single incremental read
MAX_ATTACH_LINE: int  # 4 MiB    — cap on backward widening for an oversized record

class ClaudeAnalyzer:
    def __init__(self) -> None: ...
    def feed(self, entries: list[dict]) -> bool: ...        # True if anything changed
    def activity(self) -> dict[str, Any]: ...
    def oldest_pending_age(self) -> float | None: ...

class CodexAnalyzer:
    def __init__(self) -> None: ...
    def feed(self, entries: list[dict]) -> bool: ...
    def activity(self) -> dict[str, Any]: ...
```

Guaranteed `Tail` behaviour, since this is the reason the module is shared:

- A JSONL record larger than `ATTACH_WINDOW` is still returned by `attach()`;
  the search for a record boundary widens backwards up to `MAX_ATTACH_LINE`.
- A record larger than `MAX_READ` is reassembled across successive reads; a
  trailing partial line is carried, never parsed or dropped.
- A file that shrinks below `offset` (truncation or rotation) is re-attached
  rather than read at a stale offset. Detection is by size, not inode: a
  replacement file of byte-identical length is not detected, deliberately.
- A path that has disappeared reads as empty rather than raising.
- `read_available()` drains everything on disk at call time, however large the
  append, while each individual read stays bounded by `MAX_READ`.
- Malformed lines and non-object JSON are skipped, never raised.
- The whole file is never read from the front.

`ClaudeAnalyzer.activity()` keys: `pending_tools`, `oldest_pending_age`,
`last_entry_type`, `last_prompt`, `last_assistant_text`, `asked_user_question`,
`git_branch`, `model`, `tokens`, `error`, `last_ts`, `subagents`.
`CodexAnalyzer.activity()` keys: `phase`, `last_prompt`, `last_assistant_text`,
`model`, `tokens`, `last_ts`.

### `corvidae.redact`

```python
def redact_secrets(text: str) -> str: ...
```

Covers AWS access keys, GitHub PATs, Slack tokens, `sk-ant-`/`sk-proj-`/`xai-`
keys, JWTs, bearer tokens, `password|passwd|secret|token|api_key` assignments,
credentials embedded in URLs, and PEM private keys (which redact the entire
input, not a substring). Redacted spans become `[REDACTED]`; a private-key
marker yields `[REDACTED PRIVATE KEY]`.

### `corvidae.login_agent`

```python
@dataclass(frozen=True)
class LoginAgentSpec:
    name: str; label: str; argv: Sequence[str]; working_dir: str; log_path: Path  # required
    description: str = ""; documentation: str = ""            # systemd unit metadata
    plist_path: Path | None = None; unit_path: Path | None = None
    registry_value: str = ""; tray_registry_value: str = ""   # Windows Run values
    tray_owner: str = "Windows tray app"; windows_note: str = ""
    program_label: str = "the program path"                   # error wording
    working_dir_label: str = "the working directory"
    # derived, all properties:
    plist: Path; unit: Path; unit_name: str; run_value: str; backup_tag: str

class LoginAgent(ABC):
    def __init__(self, spec: LoginAgentSpec) -> None: ...
    spec: LoginAgentSpec
    label: str                                   # "LaunchAgent" | "systemd user unit" | ...
    def installed(self) -> bool: ...
    def install(self) -> int: ...                # exit code; prints its own diagnosis
    def uninstall(self) -> int: ...

class LaunchdAgent(LoginAgent):
    def launchctl(self, *args: str) -> subprocess.CompletedProcess: ...   # override off macOS

class SystemdUserAgent(LoginAgent):
    def systemctl(self, *args: str) -> subprocess.CompletedProcess: ...   # override off Linux

class WindowsStartupAgent(LoginAgent):
    def registry(self): ...                                  # the winreg module; override
    def tray_owns_startup(self) -> bool: ...

def get_login_agent(spec: LoginAgentSpec, name: str | None = None) -> LoginAgent | None: ...

RUN_KEY: str    # r"Software\Microsoft\Windows\CurrentVersion\Run"
```

Guaranteed behaviour, since these are the reasons the module is shared:

- **The plist is built with `plistlib.dumps(dict(...))`, never string-formatted.**
  `argv` and `working_dir` are attacker-influenceable paths going into config that
  runs at every login; an XML payload in one used to inject real launchd keys.
- **systemd rejects `\n`, `\r`, and `%` in any value**, with a `ValueError`
  naming which one (`program_label`/`working_dir_label` are how a consumer makes
  that message say something useful). The first two end a directive; `%` is
  systemd's specifier prefix and would expand at load time. Refused rather than
  escaped: these are paths, so a value containing one is a broken install.
- **`install` never follows a symlink** at the config path or the temp path, backs
  up any existing file at 0600 *before* content lands, writes via
  `tempfile.mkstemp` + `os.replace`, and leaves the published file 0600.
  `uninstall` refuses a symlinked config rather than quietly removing it.
- **launchd keeps `KeepAlive`.** Its incompatibility with an app that owns the
  daemon lifecycle is intentional and documented, not a bug to soften.
- **systemd uses `Restart=on-failure`,** so `systemctl --user stop` stays
  effective.
- **`WindowsStartupAgent.install()` returns 1 and writes nothing** when
  `tray_registry_value` names a Run value that already exists. Two supervisors is
  the same double-owner mistake as launchd versus a menu-bar app. A consumer that
  ships no tray leaves `tray_registry_value` empty and the check never fires.
- **`get_login_agent` returns `None` on an unsupported platform** rather than
  raising.

Each backend's OS boundary is one overridable method, which is how all three are
tested from one machine. Overriding it is supported.

### `corvidae.descriptor`

```python
def state_dir() -> Path: ...
def descriptor_path(name: str, *, directory: Path | None = None) -> Path: ...
def publish_descriptor(name: str, payload: dict[str, Any], *,
                       directory: Path | None = None) -> Path: ...
def withdraw_descriptor(name: str, *, pid: int | None = None,
                        directory: Path | None = None) -> bool: ...
def read_descriptor(name: str, *, directory: Path | None = None) -> dict[str, Any] | None: ...
def descriptor_is_live(payload: dict[str, Any] | None, *,
                       pid_alive: Callable[[int], bool],
                       process_start_time: Callable[[int], float | None] | None = None,
                       slack: float = STARTED_SLACK) -> bool: ...

STATE_DIR_ENV: str    # "RAVENS_STATE_DIR"
```

`state_dir()`'s resolution order **is** the protocol, and every participant
(hosts included) must implement it identically:

1. `$RAVENS_STATE_DIR` when set and non-empty (`~` expanded).
2. Windows: `%LOCALAPPDATA%\Ravens`, else `~\AppData\Local\Ravens`.
3. POSIX: `$XDG_STATE_HOME/ravens`, else `~/.local/state/ravens`.

It is read per call, not cached at import, so an override set later takes effect.
It honours `XDG_STATE_HOME` even where a consumer's *own* state directory does
not — this one is shared, and replicating a project's quirk would publish where
the host is not looking, silently.

Other guaranteed behaviour:

- `publish_descriptor` writes atomically (same-directory temp file + `os.replace`,
  `fsync`ed, mode set before the replace), lands the file **0600**, creates the
  directory **0700**, and **never chmods a directory that already exists** — it is
  shared with other ravens. Publish only *after* the port is bound.
- `withdraw_descriptor` removes the file **only if its recorded `pid` matches**,
  and never raises: a corrupt or absent descriptor returns `False` and is left in
  place.
- `descriptor_is_live` requires a live recorded pid, and cross-checks `started`
  against the OS only when both a timestamp and a working `process_start_time` are
  available. A platform that cannot answer, a callable that raises, or an
  absent/zero/negative `started` all leave the pid check standing alone — reporting
  a running raven as gone is the worse failure.

What a descriptor *contains*, the menu payload, and the HTTP surface that serves
it are all deliberately out of scope: the two consumers differ on purpose.

### `corvidae.label`

```python
def sanitize_label(value: object, limit: int = MAX_LABEL) -> str: ...

MAX_LABEL: int    # 120 — the raven host's label cap
MAX_DETAIL: int   # 80  — the raven host's detail cap
```

Reduces `value` to one bounded printable line, or `""`. Strips ANSI CSI/OSC and
two-character escapes (before the control-class pass, so no printable tail like
`[31m` survives), C0/DEL/**C1** (a lone `0x9b` is an alternate CSI introducer),
bidi overrides and zero-width/invisible formatting characters, then collapses
whitespace. A non-string returns `""` rather than being coerced — `str(value)` on
a dict would put `repr()`'s attacker-chosen quoting on screen. `limit <= 0` means
no cap, which is what a caller wants when it will transform the text further
(redact, say) before capping; the function is idempotent, so sanitising again
after that costs nothing.

Redaction is deliberately *not* included: whether a menu label should have
credential shapes removed is a per-project decision. Compose the two if you want
both.

### `corvidae.model`

```python
class SessionState(str, Enum):
    ACTIVE | WORKING | WAITING_INPUT | WAITING_PERMISSION | DONE | ERROR | IDLE | ENDED

ATTENTION_STATES: set[SessionState]         # WAITING_INPUT, WAITING_PERMISSION, ERROR
STATE_RANK: dict[SessionState, int]         # lower rank = higher urgency

@dataclass
class Session:
    key: str; source: str; session_id: str; cwd: str; name: str   # required
    # ... all other fields are optional with defaults
    @property
    def attention(self) -> bool: ...
    def to_dict(self) -> dict[str, Any]: ...        # adds "rank" and "attention"
    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Session: ...   # ignores unknown keys
```

`Session.from_dict()` ignores keys it does not recognise, so a newer producer's
snapshot is readable by an older consumer. New fields always have defaults.

## Versioning

CalVer, `YYYY.MM.DD` with an optional numeric `.MICRO` suffix, matching Huginn.
The compatibility promise above is keyed to the year component.

`2026.8.1` added the login-agent, descriptor, and label surfaces listed above. It
removed and renamed nothing, so it is a drop-in replacement for `2026.7.31`.

## Not in scope

Deliberately absent, to keep the surface tight: Huginn's `Event` bus shape, the
distillation/digest layer around `redact_secrets`, config loading, and the plugin
registry.

Also considered and left out on purpose:

- **Raven menu content and the HTTP surface that serves it.** The two consumers
  differ deliberately — Huginn's endpoint is authenticated and offers actions,
  Muninn's is token-less and link-only — so a shared shape would fit neither.
- **Descriptor payload construction.** Same reason: Huginn advertises a
  `token_path`, Muninn advertises none, and a builder with a keyword per field is
  worse than two short functions.
- **Terminating-signal installation.** Both consumers turn `SIGTERM`/`SIGHUP` into
  an orderly shutdown, but by mechanisms with nothing in common: Huginn installs
  an asyncio loop handler *before* `uvicorn.Server.serve()` so uvicorn's
  `capture_signals` records it as the handler to restore (without which its
  re-raise hits the lethal default — issue
  [#43](https://github.com/tohuw/huginn/issues/43)), and sets `should_exit` so
  in-flight requests drain. Muninn has no async server and no uvicorn, and simply
  makes `signal.signal` raise `SystemExit` so a `finally` runs. A shared
  abstraction would be a callback registrar thin enough to be worth less than the
  three lines it replaces, while hiding the ordering constraint that is the entire
  substance of Huginn's version. Left duplicated.
- **Process inspection** (`pid_alive`, `process_start_time`). These need
  `libproc`/`/proc`/`GetProcessTimes` per platform, and both consumers already
  own one. `descriptor_is_live` takes them as arguments instead.

## License

Apache-2.0.
