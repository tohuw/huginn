# corvidae

Shared internals for the raven agent-monitoring projects — [Huginn](https://github.com/tohuw/huginn)
and [Muninn](https://github.com/tohuw/muninn).

Three pieces of code are genuinely reusable across both, and duplicating them
meant either a commit-pin relationship with Huginn or a second reimplementation
of edge cases that are expensive to get right once (issue
[tohuw/huginn#42](https://github.com/tohuw/huginn/issues/42)):

- **`Tail`** — a seek-from-end JSONL tailer for unbounded transcripts, with
  bounded backward widening for a record larger than the read window,
  incremental offsets, truncation/rotation recovery, and partial-line carry.
- **`redact_secrets`** — credential redaction for text leaving a transcript.
- **`Session` / `SessionState`** — one agreed session shape, so a plugin or a
  second consumer describes agent state the same way Huginn does.

`corvidae` is stdlib-only, depends on nothing, and **never imports from
`huginn`**. The dependency direction is one-way: consumers depend on `corvidae`.

```python
from corvidae import ClaudeAnalyzer, Session, SessionState, Tail, redact_secrets
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

Two explicit non-promises, so a consumer knows where the edges are:

- **`SessionState` and `activity()` dictionaries may gain members/keys within a
  year.** Do not write exhaustive `match` statements over `SessionState` without
  a default arm, and read `activity()` with `.get()`.
- **Redaction is best-effort.** `redact_secrets` recognises common credential
  *shapes*; a novel or unshaped secret can pass through. The function's
  *existence and signature* are stable, its exact pattern set is not — patterns
  are added freely (that is a fix, not a break). Never treat it as the sole
  reason it is safe to ship transcript text somewhere.

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

## Not in scope

Deliberately absent, to keep the surface tight: Huginn's `Event` bus shape, the
distillation/digest layer around `redact_secrets`, config loading, the plugin
registry, and anything that needs a running daemon.

## License

Apache-2.0.
