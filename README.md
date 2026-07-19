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
uv run huginn status         # one-shot table in the terminal
uv run huginn install-hooks  # sub-second state changes (recommended, once)
uv run huginn doctor         # environment/hook/daemon health check
```

Dashboard: cards sorted needs-you-first (permission → input → error → done →
working → idle). Tab title + favicon carry the attention count. Per card:
**jump** focuses the exact iTerm2 tab (hotkey windows included; VS Code
sessions open the workspace), **peek** shows a distilled transcript tail,
**ask** feeds the chat panel — a Q&A agent (Claude or Codex, switchable
top-right) that reads per-session digests and answers questions about what's
going on.

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
generated only when a session hits a decision point (haiku-class via
`claude -p`, debounced, rate-capped) — toggle "blurbs" in the top bar to turn
all LLM polling off; chat stays available on demand.

## How it watches

- `~/.claude/sessions/<PID>.json` — live per-process status (fsevents watch +
  pid liveness sweep; PID-reuse guarded via procStart, which is UTC vs ps's
  local time — handled).
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

## Gotchas

- Hooks fire only in sessions started *after* `install-hooks` (settings load at
  session start). Watcher-derived state covers older sessions at ~1–5s latency.
- Headless `claude -p` runs (including huginn's own blurb/chat calls) register
  session files with entrypoint `sdk-cli` — filtered out.
- Claude Code notifications use the structured `notification_type` field;
  configurable message patterns remain as a fallback for older payloads.
- "ChatGPT.app" *is* the Codex desktop app (`com.openai.codex`); the embedded
  CLI at `Contents/Resources/codex` powers the codex chat provider.

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
