# Huginn: An AI Agent Activity Console

_Developed with AI assistance. See the git history for which agents contributed._

## What Is This?

I got tired of furiously tabbing through terminals and apps to see what my
agents were doing. I tend to build things in parallel, use git worktrees to put
different workers on different project parts, and so on. I didn't like
forgetting what I was working on, and it's annoying that there aren't really
good ways to see what needs my attention (stacks of toast notifications aren't
that helpful). So I directed the construction of Huginn.

It tracks Claude and Codex terminal sessions, plus app-level presence and
activity for their desktop apps. It includes a well-tested macOS version and a
Windows version covered by automated CI but never once tested on a real Windows
machine (Codex seemed pretty sure it did a good job — I hope someone finds out
if that's true at some point).

![Huginn dashboard showing a fictional busy agent roster and the guided Ask tour](docs/huginn-demo.png)

_The privacy-safe interactive demo, with fictional sessions and transcript text._

Huginn provides deterministic tools and interfaces, including a skill your
agents can use, plus the agentic Ask interface in the console. Agents using the
skill see the same live evidence Ask receives, so which one you use is your
choice. The options in the console are deliberately few and (I hope) obvious.
You can also change most of them by asking Ask to do it for you. Note that Ask
uses **your** Claude or Codex to think, which means your usage/tokens get
consumed. It is deliberately very lightweight, though.

If you have questions, open an issue. If you and/or your best agentic pal want
to improve Huginn, please submit a PR and I'll review it in detail.

Watches **Claude Code** (first-class), **Codex** across CLI/IDE/ChatGPT Desktop
(first-class local threads), plus **ChatGPT Desktop** and **Claude Desktop**
(app-level activity tiles; general conversation content is cloud-side). Everything
runs locally; no data leaves the machine except your own `claude -p` /
`codex exec` calls, which use your existing auth.

## How Do I Use This?

**TL;DR: point your agent at this repo and it will figure it out. Then you can
just ask it how this works. :)**

```sh
uv run huginn serve          # daemon + dashboard at http://127.0.0.1:47100
uv run huginn open           # reopen the dashboard tab with a fresh auth bootstrap
uv run huginn demo           # privacy-safe interactive fictional roster
uv run huginn status         # one-shot table in the terminal
uv run huginn install-hooks  # sub-second state changes (recommended, once)
uv run huginn doctor         # environment/hook/daemon health check
```

`huginn demo` opens a self-contained product tour with fictional sessions,
transcript tails, worktree contention, title editing, sorting, view controls,
desktop tiles, and deterministic Ask answers. It never reads the live roster
API, so it is safe for screenshots, recordings, and demonstrations; closing the
tab discards all demo changes. The dashboard's **help** button opens the demo in
a separate tab and starts a guided walkthrough in the Ask panel.

### Menu bar and system tray

Huginn's menu bar is **[Roost](https://github.com/tohuw/roost)**, a separate
project, described in [Shared status menu bar](#shared-status-menu-bar) below.
Install it from its own repository; Huginn does not ship, depend on, or install
it, and publishes its status whether or not one is running.

Huginn used to carry two menu bars of its own — a Swift `Huginn.app` for macOS
(`macos/`) and a .NET 8 tray shell for Windows (`windows/Huginn.Tray`). **Both are
removed.** Each showed only Huginn, each was a whole second UI to maintain per
platform, and the two had already drifted apart. Roost shows every running raven in
one item on both platforms, and renders whatever `/api/menu` returns without
needing a change on its side.

What the old apps did, and where it went:

| Old behaviour | Now |
| --- | --- |
| Attention count in the menu bar | Roost's badge, from the same count the dashboard tab title shows |
| Open the console, focus an agent | rows Huginn publishes; Roost forwards the click back |
| **Quit Huginn** / Option-click **Restart Huginn** | `quit` and `restart` rows Huginn publishes and handles itself (`huginn/raven.py`), taking the same graceful shutdown a SIGTERM does |
| Windows **Start at login** toggle | `huginn install-agent` |
| Launching a stopped daemon | `huginn serve`, or `huginn install-agent` for start-at-login |

That last row is the one real loss, and it is deliberate. Nothing in the menu bar
starts a stopped daemon any more, because a stopped daemon has withdrawn its
descriptor — a shared menu bar cannot see it, so there is nothing to attach a
"Start Huginn" row to. Manufacturing one would mean a file naming an interpreter
for the menu bar to execute, which is exactly what `daemon.json`'s `python`/`repo`
fields needed 0600, an ownership check, and a group/world-writable check on every
parent directory to make safe. Multiplying that into a process shared across every
raven is worse than not having the button. Starting things at login is what
`install-agent` is for, and it puts the exec path in launchd, systemd, or the
Windows `Run` key — supervisors already built to hold one.

Native Windows support (AppData-backed state, Claude/Codex discovery, portable
hooks, process/window focus, WSL session discovery) is unaffected by the tray's
removal: it lives in `huginn/platform/windows.py` and the sources, not in the
deleted shell. Windows Terminal focus is window-level; exact selection of an
arbitrary existing tab remains a documented limitation. See
[WINDOWS.md](WINDOWS.md).

### Shared status menu bar

Huginn also publishes itself as a **raven**: a participant in one shared status
menu bar that can show several tools at once. It is opt-out only in the sense
that a menu bar has to be installed separately — Huginn publishes whether or not
one is running, and publishing is best-effort, so nothing about the console
depends on it.

The menu bar itself is **[Roost](https://github.com/tohuw/roost)**, a separate
Apache-2.0 project: one macOS menu bar / Windows tray item that renders whichever
ravens are running — today Huginn and [Muninn](https://github.com/tohuw/muninn).
Its `SPEC.md` is normative for the wire format below, and nothing here is specific
to it: Huginn publishes a descriptor and serves a menu, and any host implementing
that protocol would work. Install it from its own repository; Huginn does not
depend on it, ship it, or install it.

While the daemon is serving it writes one descriptor:

| | |
| --- | --- |
| Where | `$RAVENS_STATE_DIR`, else `%LOCALAPPDATA%\Ravens` on Windows, else `$XDG_STATE_HOME/ravens`, else `~/.local/state/ravens` |
| File | `huginn.json`, mode 0600, written atomically after the port is bound |
| Removed | on a clean daemon exit; a descriptor left by a crash is refused by the menu bar, which checks the recorded pid and start time |

Note this directory honours `XDG_STATE_HOME` while Huginn's own state directory
(`~/.local/state/huginn`) does not. That is deliberate: the ravens directory is
shared with the menu bar and any other participant, so resolving it Huginn's way
would publish where nothing is looking.

The descriptor names Huginn's port, its protocol range (`min_api`/`max_api` — a
range, never an equality, for the same reason plugins declare one), and the path
to Huginn's ordinary API token. The menu bar reads that token fresh per request
and calls two authenticated routes:

- `GET /api/menu` — a declarative menu: a triage headline, sessions needing
  attention, worktree contention, what is working, and dismissable ended
  sessions, plus a badge carrying the same attention count the dashboard tab
  title shows. The menu bar renders these labels without interpreting them.
- `POST /api/menu/action` — an action id from that menu, handed back unchanged.
  `focus:<key>` jumps to a session exactly as the dashboard's **jump** does,
  `dismiss:<key>` removes an ended card, and `open-console` opens the dashboard
  with a fresh auth bootstrap. An id Huginn no longer recognises — a session that
  ended between the menu being drawn and clicked — is refused, never guessed at.
  `quit` and `restart` stop or restart the daemon, replacing what the deleted
  native apps did. Both go through the *same* graceful shutdown a SIGTERM gets, so
  the descriptor, `daemon.json`, and the token are withdrawn on the way out rather
  than orphaned by a kill. Both also reply *before* the process unwinds: a raven
  that exited inside its own request handler would make a successful quit look to
  the menu bar like an action that failed.

None of these ids mean anything to the menu bar. `quit` is forwarded exactly as
`focus:claude:1` is, which is why adding it needed no change in Roost and no
protocol version bump — and why there is no `start`: see the note under
[Menu bar and system tray](#menu-bar-and-system-tray).

Both routes sit behind the same token and `Origin`/`Host` checks as the rest of
the API; there is no unauthenticated menu surface. Session names, titles, and
blurbs are sanitised and credential-redacted before they become menu labels, on
Huginn's side, rather than relying on the menu bar to clean up after it.

### Start at login

```sh
uv run huginn install-agent     # uv run huginn uninstall-agent to remove
```

On macOS this also builds `~/Applications/Huginn.app`, a managed Finder/Spotlight
entry that opens the running dashboard. `uninstall-agent` removes it, and Huginn
refuses to overwrite an unrelated bundle at that location.

The mechanism and its restart policy follow the platform:

| Platform | Mechanism | On exit |
| --- | --- | --- |
| macOS | `~/Library/LaunchAgents/is.tohuw.huginn.plist` | `KeepAlive` restarts it |
| Linux | `~/.config/systemd/user/huginn.service` | `Restart=on-failure` only |
| Windows | `HKCU\...\CurrentVersion\Run\HuginnDaemon` | not restarted |

The differences are deliberate. launchd's `KeepAlive` restarts the daemon even
after a clean exit. **That means a Quit from the menu bar will not stick while the
launchd agent is installed** — the daemon shuts down cleanly, withdraws its
descriptor, and launchd starts it straight back up. That is the agent doing its
job, not a bug, and it is not something a menu row can mediate: run
`uv run huginn uninstall-agent` if you want quitting to be final. The systemd unit
uses `Restart=on-failure` instead so `systemctl --user stop huginn` stays
effective; add `loginctl enable-linger $USER` to keep it running between logins on
a headless host. Windows does not restart it at all.

On Windows, `install-agent` writes `HuginnDaemon` and still refuses to run while a
`Huginn` value — the removed tray's own startup entry — is present under the same
`Run` key. The guard is kept because a machine that ran the old portable tray still
has that value, and two autostarts would resurrect a daemon you just quit. See
[WINDOWS.md](WINDOWS.md).

### Agent access

Install the CLI on `PATH` from this checkout:

```sh
uv tool install --editable .
```

Agent skills may also run the checkout-local command without a global install:

```sh
uv run --directory /path/to/huginn huginn roster
```

That is the preferred fallback for an agent whose shell cannot resolve
`huginn`: it keeps the CLI's stable public boundary available without modifying
the user's `PATH`.

Huginn includes one canonical cross-agent skill at
`.agents/skills/huginn/SKILL.md`; `.claude/skills/huginn` links to it for
Claude Code. Install or link that directory into `~/.agents/skills/huginn`
and `~/.claude/skills/huginn` to make it available from every repository.
The skill uses the stable, authenticated CLI instead of teaching agents to
read daemon internals:

```sh
huginn roster --attention
huginn triage
huginn inspect @session-name
huginn focus @session-name
huginn history @session-name   # recorded state transitions, e.g. a brief wrong-state flip
```

Local terminal sessions are observe-only by default. Steering is a separate,
explicit capability with a two-stage confirmation:

```sh
huginn authority @session-name steer
huginn send @session-name "continue with the focused test"
huginn interrupt @session-name
huginn authority @session-name observe
```

`send` accepts one bounded line, previews that exact line, and requires typing
`yes` before the daemon submits it. `interrupt` separately previews Ctrl-C.
Confirmations are random, one-use, process-local, and expire after 60 seconds;
authority is bound to both the roster key and session ID so PID reuse cannot
inherit control. There is no generic command runner. Exact-tab steering is
currently available for live Claude/Codex CLI sessions mapped to iTerm2; Windows
and editor-hosted sessions fail closed until an exact target can be guaranteed.

Dashboard: session cards sort needs-you-first (permission → input → error →
done → working → idle); ambient desktop-app tiles form a separate group below
them. A persistent compact list view is available from the top bar. Tab title +
favicon carry only actionable session attention, never app activity. Cards use
native display scaling, show four lines of session evidence by default, preserve
expanded evidence across roster polls, and keep their action rails aligned. Per session:
**jump** focuses the exact iTerm2 tab on macOS (hotkey windows included; VS Code
sessions open the workspace; Windows Terminal currently focuses the owning window),
**peek** shows a distilled transcript tail,
**ask** feeds the chat panel — a Q&A agent (Claude or Codex, switchable
top-right) that reads current per-session digests and answers questions about
what's going on. Ask stays in that monitoring scope; it can also toggle blurbs,
switch its provider, change cards/list view, title a card, and hide its own
panel. Ask answers render headings, lists, emphasis, inline code, and fenced
code blocks through DOM construction without HTML injection. The pencil edits a
short ephemeral card title; absent a manual title,
the configured LLM may guess one from current session evidence. Titles belong
to that card only and disappear when it does. Dashboard settings persist across
reloads and synchronize across open tabs.

For open-ended questions ("which session is fixing the login bug?"), Ask's
roster includes each session's current title or blurb alongside its name and
state — enough topic signal to pick the right digest file(s) to read without
opening every one. That cached label is never treated as evidence of current
state or a blocker; only the transcript and live state are.

Ask can also drive the jump and peek buttons directly — "jump @session-name"
focuses that session's terminal exactly as clicking jump would, and "peek
@session-name" expands that card's transcript tail (also echoed in the chat
reply). Both resolve @name the same way titling does: an exact match, or a
unique prefix.

## Session states

| state | meaning | derived from |
|---|---|---|
| `working` | agent is running | status file `busy`/`shell`, transcript flow, hooks |
| `waiting_permission` | blocked on a permission prompt | Claude Notification; Codex approval event when emitted; fallback: pending tool use >20s |
| `waiting_input` | explicitly asked you something | elicitation/Stop hooks + transcript (AskUserQuestion) |
| `done` | turn finished cleanly | Stop hook, busy→idle after turn end |
| `error` | API error or died mid-work | transcript error lines, dead pid while working |
| `idle` / `ended` | nothing happening / process gone | status file, pid liveness |

Desktop tiles are a different observation class. `active` means the native app
is running and its Electron renderer recently wrote local state; `idle` means
the app is present without that recent signal. Renderer activity can come from
scrolling or other user interaction as well as generation, so app tiles are
visually separated, sorted outside the urgency queue, and never raise attention.
The `apps` control—or Ask commands such as “hide desktop presence”—can remove
that section entirely when app-level context is not useful. Jumping to a desktop
tile restores the app even when it has been closed to the tray, which is the
state both Claude Desktop and ChatGPT spend most of their time in.

Rule-based states are always on and cost nothing. Automatic LLM titles and
one-line **blurbs** remain enabled by default; toggle "blurbs" in the top bar
to disable or re-enable both while Ask remains available on demand. Automatic calls are
coalesced per session, cached by exact evidence, limited to six per minute and
200 per UTC day (the daily counter survives restarts), and stopped by a
provider-wide failure circuit. Permanent model/authentication failures remain
stopped until the provider/model setting changes or automatic text is toggled.
Blurbs are cleared on state changes and are deliberately excluded from Ask's
evidence so an old summary cannot invent a current blocker.

The live roster expires non-actionable records, but never merely ages out an
open terminal session. Claude CLI cards remain for the life of their process;
Codex CLI cards require repeated roster misses plus a live-process/TTY poll
confirming the tab is gone. Completed `codex exec` jobs remain for 30 seconds,
and persistent editor backends still leave the view after their idle cutoff.

Huginn also computes deterministic triage from the same roster used by Ask.
When two active local agents resolve to the same Git worktree, the dashboard,
CLI, and Ask context surface that contention explicitly. Separate worktrees are
kept separate even when their repositories share the same basename.

## How it watches

- `~/.claude/sessions/<PID>.json` — live per-process status (fsevents watch +
  pid liveness sweep; PID reuse guarded by comparing Claude's UTC `procStart`
  to the OS process creation epoch, obtained through native process APIs where
  available). Direct child shells are counted separately; a
  completed assistant turn remains done even when background shells survive.
- `~/.claude/projects/*/<sessionId>.jsonl` — transcripts, tailed seek-from-end
  (64KB normal attach window, bounded widening across oversized JSONL records,
  incremental offsets; never read front-to-back).
- `~/.codex/state_5.sqlite` — thread index, polled read-only. Successful reads
  refresh a transactionally consistent SQLite online-backup snapshot; a recent
  snapshot is used if WAL/shm access is temporarily blocked, and an unavailable
  or expired snapshot fails closed rather than returning a torn roster. Rollout JSONLs tailed for
  `task_started`/`task_complete` turn boundaries.
- ChatGPT Desktop — local Codex threads share `CODEX_HOME` and therefore use
  the same first-class scanner above. A separate app tile reports native
  process presence and recent Electron activity on macOS and Windows without
  attempting to extract cloud conversation content.
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
  Huginn defensively recognizes the command/file/permission approval families
  exposed by the installed Codex binary, but no approval event has appeared in
  the captured local rollout fixtures. Therefore Codex still has a documented
  worst-case 20-second pending-tool fallback when those events are absent.

See [docs/hooks.md](docs/hooks.md) for the implementation-level walkthrough:
the forwarder's fail-open design, the append-only/idempotent install strategy,
exactly which hook event maps to which `SessionState`, and how hook evidence
is arbitrated against transcript/status-file/poll evidence (origin priority +
grace window) when they disagree.

## Gotchas

- Hooks fire only in sessions started *after* `install-hooks` (settings load at
  session start). Watcher-derived state covers older sessions at ~1–5s latency.
- Huginn's headless `claude -p` calls use `--no-session-persistence`, so
  automatic titles, blurbs, and Ask do not become fake conversation history.
  Other headless SDK/CLI sessions are still filtered from the live roster by
  entrypoint. Provider children also carry Huginn's owned
  `HUGINN_INTERNAL=1` marker and are tracked by PID, so the recursion guard
  does not depend solely on Claude's entrypoint convention.
- Claude Code notifications use the structured `notification_type` field;
  `idle_prompt` settles to done rather than raising attention, while explicit
  elicitation remains waiting-input. Configurable message patterns remain as
  a fallback for older payloads.
- "ChatGPT.app" *is* the Codex desktop app (`com.openai.codex`); the embedded
  CLI at `Contents/Resources/codex` powers the codex chat provider.
- Huginn cannot track conversations open in `claude.ai` or `chatgpt.com`
  browser tabs. Without an explicit browser integration, that would require
  fragile screen scraping tied to frequently changing web interfaces. Huginn
  deliberately avoids that maintenance and privacy boundary.

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
  logged) traded for an HttpOnly, `SameSite=Strict` session cookie. Fragments
  can remain in browser history/session restoration until Huginn strips them,
  so bootstrap URLs should not be pasted into chats, issues, or recordings. `GET /`
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

The shared status menu bar authenticates with that same token, read from the
`token_path` its descriptor advertises — so it is inside the token boundary, not
an exception to it. The descriptor itself carries no secret; it is 0600 for the
same reason `daemon.json` is, because another process reads a port and a token
path out of it and acts on them.

If the daemon restarts, the API token rotates, but this is continuity rather
than revocation: the separate on-disk refresh credential intentionally persists
across restarts. An open, previously authorized tab silently refreshes its
HttpOnly session cookie; `huginn open` remains the bootstrap path for a new
browser profile or a cleared-cookie session.

Peek, blurbs, and Ask share one bounded transcript-distillation seam. Before
that evidence reaches the dashboard or an LLM provider, Huginn redacts common
credential shapes (including AWS and GitHub tokens, bearer/JWT values, secret
assignments, credential-bearing URLs, and private keys). Session metadata
inserted into LLM prompts is normalized, redacted, and length-bounded too. This
is defense in depth, not a general secret scanner: avoid putting credentials in
prompts or agent output in the first place.

## Config

`~/.config/huginn/config.toml` — server port, automatic-text
enable/provider/model/minute and daily budgets, Ask model, notification
patterns, poll cadences, ended-card TTL, `doctor.max_lag_s`. All editable from
the dashboard settings too (PUT `/api/settings`).

## Plugins

Huginn discovers installed plugin distributions through the standard
`huginn.plugins` Python entry-point group. Plugins can contribute Ask providers
and long-running session sources without being copied into the core package.
They are trusted native code: installation is the explicit trust boundary, and
Huginn does not execute modules found by scanning arbitrary directories.

An entry point returns a `huginn.plugins.PluginSpec` with API version `1`. A
plugin may declare the range of APIs it supports (`min_api`/`max_api`, both
defaulting to `api_version`), and is loaded when that range overlaps core's; a
non-overlapping range is reported loudly on the daemon log and as a `huginn
doctor` error rather than skipped silently. Provider options appear dynamically
in the dashboard; source plugins receive a narrow `SourceContext` for namespaced
session upserts, removals, configuration, and redacted health reporting.
`GET /api/plugins` reports loaded plugins and isolated load failures.

A separate entry-point group, `huginn.policy`, declares a `ModelPolicy` — a
fail-closed allowlist of model ids and an optional required provider that every
LLM call Huginn makes must satisfy. Policies intersect rather than union, so an
installed policy can only narrow what is permitted and nothing (another policy,
config, the dashboard, or an Ask command) can widen it; a refusal surfaces the
policy's own reason verbatim and never substitutes a different model. With no
policy installed, every model is permitted — the default is unchanged. This is a
contract, not a sandbox: see [docs/plugins.md](docs/plugins.md#model-policy) for
the scope it does and does not cover.

Install a plugin package into Huginn's active environment (for example,
`uv pip install -e /path/to/plugin`) to make it discoverable. See
[the plugin author guide](docs/plugins.md) for the contract and a minimal
package example.

## Shared internals: corvidae

Several parts of Huginn are reusable outside it, and were being reimplemented
elsewhere purely because nothing promised they would keep working. They now live
in **[`corvidae`](packages/corvidae/)**, a stdlib-only package in this repo that
is published separately — [on PyPI](https://pypi.org/project/corvidae/), Apache-2.0
— and depends on nothing (least of all on Huginn):

- `Tail` — the seek-from-end JSONL transcript tailer, with its awkward edges
  (a record larger than the read window, truncation below the stored offset,
  rotation, partial-line carry) — plus `ClaudeAnalyzer` / `CodexAnalyzer`, the
  per-dialect readers that turn tailed records into an activity dictionary.
- `redact_secrets` — credential redaction for text leaving a transcript.
- `Session` / `SessionState` / `STATE_RANK` / `ATTENTION_STATES`.
- `LoginAgentSpec` / `get_login_agent` and the launchd/systemd/Windows backends —
  the start-at-login machinery behind `huginn install-agent`, including the
  plist/unit injection refusals and the 0600 write-with-backup discipline.
- `state_dir` / `publish_descriptor` / `withdraw_descriptor` /
  `descriptor_is_live` and `sanitize_label` — the shared-directory half of the
  raven protocol, plus the sanitiser for untrusted menu text. What Huginn's
  descriptor *says*, its menu, and its `/api/menu` endpoint stay here: they are
  authenticated and offer actions, which the other raven deliberately does not.

**These names are stable within a CalVer year.** The exact surface, signatures,
guaranteed behaviour, and the explicit non-promises are documented in
[packages/corvidae/README.md](packages/corvidae/README.md#stability-contract).
Anything not listed there is an implementation detail.

Huginn keeps all of them working from their original module paths — `huginn.model`,
`huginn.sources.transcript`, `huginn.llm.context`, `huginn.agent_install`,
`huginn.raven` — so existing plugins and forks need no changes. Nothing else in `huginn.*` carries a compatibility promise; the
plugin contract in `huginn.plugins` has its own, versioned separately (see
[API version ranges](docs/plugins.md#api-version-ranges)).

## Versioning

Huginn uses calendar versioning. Package versions follow `YYYY.MM.DD`, with a
numeric `.MICRO` suffix for additional releases on the same day. Annotated Git
tags add a leading `v`, for example `v2026.07.29.3`. `corvidae` versions the same
way, independently, and its compatibility promise is keyed to the year component.

## Dev

```sh
uv run pytest -q     # huginn's tests + the shared package's
```

Architecture: sources (fsevents watchers + pollers) → event bus → one reducer
(`huginn/state.py`, pure transition rules, unit-tested) → SSE → vanilla-JS
dashboard. No build step, three runtime dependencies (fastapi, uvicorn,
watchfiles) plus the in-repo `corvidae`.

**The console keeps itself current without a reload**, which takes more than the
event stream. SSE is the fast path; a snapshot poll reconciles what the stream
missed, in *both* directions — it adds sessions and removes them, but only once
`/api/sessions` reports `complete`, meaning every roster source has either
scanned or declined to run since the daemon booted. Until then absence just
means nobody has looked yet, and removing on it would blank a roster still being
assembled. The stream is also re-established after an HTTP error, which
`EventSource` never does by itself: a daemon restart rotates the token, so the
next connection answers 401 and the feed would otherwise stay dead for the life
of the page. Returning to a background tab resyncs immediately rather than
waiting out a throttled timer.

The repo is a `uv` workspace: `packages/corvidae/` is a second distributable, so
`uv build --all-packages` builds both wheels and `uv sync` resolves corvidae from
the checkout rather than an index.

That workspace layout used to be a release prerequisite: Huginn's wheel declares
`corvidae` as an ordinary dependency, and a consumer installing Huginn by Git
reference (`huginn @ git+https://…@<sha>`) cannot resolve a workspace member from
that URL. **That is settled — `corvidae` is on PyPI**, so a Git-reference consumer
resolves it from the index like any other dependency and needs no
`[tool.uv.sources]` override. Recorded because the constraint is easy to
reintroduce: a *new* workspace member that Huginn depends on would have the same
problem again, and the fix is publishing it, not pinning around it.

In-repo development is unaffected either way — `uv sync` still resolves corvidae
from the checkout, so a local change to it is picked up without a release.
