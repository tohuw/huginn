# Hooks and agent interop

This is the implementation-level companion to the "How it watches" section of
the README: how Claude Code and Codex hook events actually reach the reducer,
and how they interact with the other evidence sources feeding session state.

## The forwarder

`huginn-hook` (`huginn/hooks/cli.py`) is a tiny console script, not a daemon
plugin. Claude Code and Codex invoke it as a subprocess for each configured
hook event, piping the event's JSON payload to stdin. It:

1. Reads the daemon's port from `~/.local/state/huginn/port` (falls back to
   `47100` if that file is missing — the daemon hasn't started yet or was
   never installed).
2. Reads the auth token from `~/.local/state/huginn/token`.
3. POSTs the payload verbatim to `http://127.0.0.1:<port>/api/hook/<source>/<event>`
   with a 1-second timeout.
4. Swallows every failure (`OSError`, `URLError`, bad JSON, missing files) and
   always exits `0`.

That last point is the load-bearing design decision: a hook is a side channel,
never a gate. If the daemon is stopped, mid-restart, or the state directory is
temporarily unreadable, the coding agent's own turn must proceed exactly as if
Huginn did not exist. Nothing about hook delivery is retried or queued client
side — a dropped hook is simply evidence Huginn never receives; other sources
(status files, transcript tailing, polling) exist precisely so that no single
signal is load-bearing.

## Installation

`huginn install-hooks` (`huginn/hooks/install.py`) edits
`~/.claude/settings.json` and `~/.codex/hooks.json` directly — there is no
Claude/Codex API for hook registration. The edit is:

- **Append-only.** Existing entries for other tools are left untouched;
  matching is done by searching each event's hook list for a command
  containing the literal substring `huginn-hook`, not by array position.
- **Idempotent.** Re-running `install-hooks` is a no-op if Huginn's entries are
  already present for every event it wants.
- **Backed up.** Before any write, the target file is copied to
  `<file>.huginn-bak.<unix-ts>`.
- **Atomic.** Writes go to a `.tmp` file followed by `os.replace()`.

`uninstall-hooks` reverses this by removing only hook entries whose command
contains `huginn-hook`, leaving every other entry (including ones we don't
recognize the shape of) alone.

Claude Code registers all five events Huginn understands: `SessionStart`,
`UserPromptSubmit`, `Notification`, `Stop`, `SessionEnd`. Codex's hook-event
enum (as of CLI 0.145) only has three: `SessionStart`, `UserPromptSubmit`,
`Stop` — `install-hooks` does not attempt to register `Notification` or
`SessionEnd` for Codex, since the binary has nothing to fire them from.
Claude's hooks are installed with `"async": true`; Codex 0.145 silently skips
hooks marked async ("not supported yet"), so Codex's are installed
synchronously instead — safe only because the forwarder's own worst case is a
round trip against a local daemon capped by its 1-second timeout.

## From HTTP request to session state

`POST /api/hook/{source}/{event}` (`huginn/server/app.py`) does three things
before handing off to the reducer:

1. Records the hit in `daemon.hook_hits` (exposed at `GET /api/hook-stats`, so
   you can verify a given hook is actually firing in your environment rather
   than guessing).
2. For Claude's `Stop` event specifically, reads any transcript lines that
   arrived since the last tail read and asks the `ClaudeAnalyzer` whether the
   assistant's last turn called `AskUserQuestion`. This disambiguates "the
   agent finished a turn cleanly" (`DONE`) from "the agent is now blocking on
   you" (`WAITING_INPUT`) — a distinction the `Stop` event alone cannot make,
   since Claude fires `Stop` in both cases.
3. Wraps the payload in an `Event(kind=f"hook.{source}", ...)` and pushes it
   onto the bus for the reducer to consume asynchronously. The HTTP handler
   itself never blocks on reducer logic.

The reducer's Claude and Codex hook handlers
(`Reducer._on_hook_claude` / `_on_hook_codex` in `huginn/state.py`) then map
events to `SessionState`:

| Event | Claude | Codex |
|---|---|---|
| `SessionStart` | touches `last_activity` | touches `last_activity` |
| `UserPromptSubmit` | → `WORKING`, records `last_prompt` | → `WORKING` |
| `Notification` | → `WAITING_PERMISSION` or `WAITING_INPUT` (see below) | not fired |
| `Stop` | → `WAITING_INPUT` if `asked_question`, else `DONE` | → `DONE` (no tail disambiguation available at hook time) |
| `SessionEnd` | → `ENDED` | not fired |

Claude's `Notification` handling has a priority order. Claude Code 2.x sends a
structured `notification_type` field (`permission_prompt`, `idle_prompt`,
`elicitation_dialog`, `elicitation_complete`, `auth_success`, ...) which is
checked first. If that field is absent (older Claude Code builds, or a
third-party hook forwarder emitting a bare message), Huginn falls back to
substring-matching `data["message"].lower()` against the configurable
`patterns.permission` list (default: `permission`, `approve`, `authoriz`,
`allowed`) — a match means `WAITING_PERMISSION`, anything else means
`WAITING_INPUT`. This fallback only ever inspects the hook payload's
`message` field; nothing in Huginn reads terminal tab titles, window titles,
or any other window-manager metadata into session state.

## Hooks vs. every other evidence source

Hooks are one of four state-origin classes the reducer arbitrates between,
ranked by `_ORIGIN_PRIORITY` in `huginn/state.py`:

```
hook (3) > transcript (2) > statusfile/poll (1) > timeout/init (0)
```

A higher-priority origin can always overwrite a lower one. A lower-priority
origin can only overwrite a higher one once the current state has aged past
30 seconds — except `WORKING` from a status file, which always wins
immediately, because "the process is busy" from `busy`/`shell` in Claude's own
status file is about as strong as evidence gets.

On top of that, a state set by a hook holds a **3-second grace window**
(`HOOK_GRACE_S`) against any contradicting lower-priority evidence, regardless
of the 30-second aging rule above. This exists because hooks are
edge-triggered and precise (Claude/Codex fire them at the exact moment
something changes) while the status-file poll and transcript tail are
lag-prone by comparison — without the grace window, a slightly-stale poll
landing a few hundred milliseconds after a hook could immediately clobber the
hook's more accurate state.

Practically, this means: hooks give you low-latency, exact-moment state
transitions when installed and firing; the transcript tail and status-file
poll are what keep the roster correct when a hook is missing, delayed, or (for
Codex) doesn't exist for that event at all. Neither is a strict superset of
the other, and `GET /api/hook-stats` plus `GET /api/health` are the two
endpoints to check when a session's state looks wrong — the first tells you
whether the hook fired at all, the second tells you whether the source that
would otherwise have caught it is healthy.
