# huginn

Local monitor for AI coding-agent sessions. One pinned browser tab instead of
furiously tabbing through iTerm2 to find out which agent needs you.

Watches **Claude Code** (first-class), **Codex** (first-class), and
**Claude Desktop** (activity tile only — its content is cloud-side). Everything
runs locally; no data leaves the machine except your own `claude -p` /
`codex exec` calls, which use your existing auth.

## Use

```sh
uv run huginn serve          # daemon + dashboard at http://127.0.0.1:47100
uv run huginn open           # reopen the dashboard tab with a fresh auth bootstrap
uv run huginn status         # one-shot table in the terminal
uv run huginn install-hooks  # sub-second state changes (recommended, once)
uv run huginn doctor         # environment/hook/daemon health check
```

### macOS menu-bar app

Build the native menu-bar app into `~/Applications`:

```sh
macos/build-app.sh
open ~/Applications/Huginn.app
```

The app owns the daemon lifecycle, shows an attention count in the menu bar,
opens the web console, and focuses an agent when you select its permission,
input, or error entry. **Quit Huginn** stops the daemon. Remove the launchd
version first with `uv run huginn uninstall-agent`; its `KeepAlive` policy is
intentionally incompatible with app-owned shutdown.

### Agent access

Install the CLI on `PATH` from this checkout:

```sh
uv tool install --editable .
```

Huginn includes one canonical cross-agent skill at
`.agents/skills/huginn/SKILL.md`; `.claude/skills/huginn` links to it for
Claude Code. Install or link that directory into `~/.agents/skills/huginn`
and `~/.claude/skills/huginn` to make it available from every repository.
The skill uses the stable, authenticated CLI instead of teaching agents to
read daemon internals:

```sh
huginn roster --attention
huginn inspect @session-name
huginn focus @session-name
```

Dashboard: cards sorted needs-you-first (permission → input → error → done →
working → idle), with a persistent compact list view available from the top
bar. Tab title + favicon carry the attention count. Per session:
**jump** focuses the exact iTerm2 tab (hotkey windows included; VS Code
sessions open the workspace), **peek** shows a distilled transcript tail,
**ask** feeds the chat panel — a Q&A agent (Claude or Codex, switchable
top-right) that reads current per-session digests and answers questions about
what's going on. Ask stays in that monitoring scope; it can also toggle blurbs,
switch its provider, change cards/list view, and hide its own panel. Dashboard
settings persist across reloads and synchronize across open tabs.

## States

| state | meaning | derived from |
|---|---|---|
| `working` | agent is running | status file `busy`/`shell`, transcript flow, hooks |
| `waiting_permission` | blocked on a permission prompt | Notification hook; fallback: pending tool_use >20s |
| `waiting_input` | asked you something / idle turn | Notification/Stop hooks + transcript (AskUserQuestion) |
| `done` | turn finished cleanly | Stop hook, busy→idle after turn end |
| `error` | API error or died mid-work | transcript error lines, dead pid while working |
| `idle` / `ended` | nothing happening / process gone | status file, pid liveness |

Rule-based states are always on and cost nothing. One-line LLM **blurbs** are
generated only when a session hits a decision point (debounced and rate-capped)
— toggle "blurbs" in the top bar to turn all LLM polling off; chat stays
available on demand. Blurbs are cleared on state changes and are deliberately
excluded from Ask's evidence so an old summary cannot invent a current blocker.

The live roster expires non-actionable records: idle sessions and completed
interactive Codex turns remain for 5 minutes, while completed `codex exec`
jobs remain for 30 seconds. Persistent editor backends and one-off scratchpad
probes therefore leave the live view without hiding attention states.

## How it watches

- `~/.claude/sessions/<PID>.json` — live per-process status (fsevents watch +
  pid liveness sweep; PID-reuse guarded via procStart, which is UTC vs ps's
  local time — handled). Direct child shells are counted separately; a
  completed assistant turn remains done even when background shells survive.
- `~/.claude/projects/*/<sessionId>.jsonl` — transcripts, tailed seek-from-end
  (64KB attach window, incremental offsets; never read front-to-back).
- `~/.codex/state_5.sqlite` — thread index, polled read-only (copy-to-cache
  fallback when WAL/shm access is blocked); rollout JSONLs tailed for
  `task_started`/`task_complete` turn boundaries.
- Hooks (optional, recommended): `huginn-hook` forwards Claude Code and Codex
  hook events to `POST /api/hook/...` with 0.2s connect timeout — if the
  daemon is down the hook is a no-op; sessions never block. Codex hooks are
  installed sync (its `async` hooks are skipped as of 0.145). Installation is
  append-only + idempotent into `~/.claude/settings.json` / `~/.codex/hooks.json`
  (backups written; `uninstall-hooks` removes exactly ours).
- Codex's hook-event enum only has `SessionStart`/`UserPromptSubmit`/`Stop`
  (no `Notification`/`SessionEnd` — `install-hooks` only registers the three
  that exist). Explicit choice (issue #20): these are kept and do feed the
  reducer for lower-latency working/done transitions, layered on top of the
  poll/rollout source via the same origin-priority rules as Claude's hooks —
  not a replacement for it, and a safe no-op for a thread the poller hasn't
  discovered yet. `GET /api/hook-stats` (issue #2) shows real fire counts.

## Gotchas

- Hooks fire only in sessions started *after* `install-hooks` (settings load at
  session start). Watcher-derived state covers older sessions at ~1–5s latency.
- Headless `claude -p` runs (including huginn's own blurb/chat calls) register
  session files with entrypoint `sdk-cli` — filtered out.
- Claude Code notifications use the structured `notification_type` field;
  configurable message patterns remain as a fallback for older payloads.
- "ChatGPT.app" *is* the Codex desktop app (`com.openai.codex`); the embedded
  CLI at `Contents/Resources/codex` powers the codex chat provider.

## Security

The daemon binds `127.0.0.1` only. API routes require a per-restart token
(`~/.local/state/huginn/token`, mode 0600), except the session-refresh route,
which requires a separate persistent credential stored as an HttpOnly,
path-scoped cookie (`~/.local/state/huginn/refresh-token`, mode 0600). What
this does and doesn't protect against:

- **Protects against:** another process on your machine — a script, a
  compromised dependency, a stray webpage your browser has open — making
  requests to the daemon's API without your consent. The token bootstraps
  into the browser via a URL fragment (`#t=...`, never sent to the server or
  logged) traded for an HttpOnly, `SameSite=Strict` session cookie; `GET /`
  itself carries no secret, so it's safe for any local process to fetch. The
  refresh credential can only mint a new session cookie; it is never accepted
  by other API routes. `Origin`/`Host` header checks reject cross-origin and
  DNS-rebinding requests on top of that.
- **Does not protect against:** another process running *as you* that can
  read your files — it can read `~/.local/state/huginn/token` directly (same
  permission boundary as reading your Claude transcripts), so this isn't
  privilege isolation between processes owned by the same user. If that's
  your threat model, huginn isn't the layer defending against it; your OS
  user/process sandboxing is.

If the daemon restarts, the API token rotates. An open, previously authorized
tab silently refreshes its HttpOnly session cookie; `huginn open` remains the
bootstrap path for a new browser profile or a cleared-cookie session.

## Config

`~/.config/huginn/config.toml` — server port, LLM enable/provider/models/caps,
notification patterns, poll cadences, ended-card TTL. All editable from the
dashboard settings too (PUT `/api/settings`).

## Dev

```sh
uv run python -m unittest discover -s tests   # reducer + analyzer tests
```

Architecture: sources (fsevents watchers + pollers) → event bus → one reducer
(`huginn/state.py`, pure transition rules, unit-tested) → SSE → vanilla-JS
dashboard. No build step, three dependencies (fastapi, uvicorn, watchfiles).
