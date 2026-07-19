# Codex verification checklist

## Results (2026-07-18)

- **#2 resolved:** live traffic recorded `codex.SessionStart`,
  `codex.UserPromptSubmit`, and `codex.Stop`. Codex CLI 0.144.6's embedded
  hook-event enum contains those events but not `Notification` or
  `SessionEnd`; the installer now registers only the supported three and
  removes stale huginn entries for the other two. The same binary still says
  async hooks are unsupported, so Codex hooks remain synchronous.
- **#1 resolved:** current Claude Code provides a structured
  `notification_type` (`permission_prompt`, `idle_prompt`, and elicitation
  variants). The reducer now uses it instead of guessing from message prose;
  configurable message patterns remain as a fallback for older payloads.

The sections below are retained as the original verification procedure and
for future regression checks.

Two huginn issues were initially left open because they needed real usage data that couldn't
be manufactured from this machine (no `codex` CLI on PATH, only the
GUI-only ChatGPT desktop app) or in one sitting:

- **[#2](https://github.com/tohuw/huginn/issues/2)** — which of Codex's
  `SessionStart`/`UserPromptSubmit`/`Notification`/`Stop`/`SessionEnd` hooks
  actually fire.
- **[#3](https://github.com/tohuw/huginn/issues/3)** (implemented, unverified)
  — `waiting_permission`/`waiting_input` detection was wired up from event
  *type strings* pulled out of the Codex desktop binary, not from an observed
  payload. No local approval/question event has ever fired, so the exact
  field shapes are a best guess.

This is a paced checklist for exercising Codex normally and checking what
huginn actually saw. Nothing here needs code changes going in — it's data
collection. Section 4 covers the other open issue (**#1**, Claude Code
Notification patterns) since it's the same "just use the tool and check
back" shape, even though it's not Codex-specific.

## Setup (once)

- [ ] Daemon running: `launchctl list | grep is.tohuw.huginn` (should show a
      pid). If not: `huginn install-agent`, or just `huginn serve`.
- [ ] Codex hooks installed: `huginn install-hooks` (idempotent, safe to
      re-run). Confirms `~/.codex/hooks.json` has huginn entries.
- [ ] Grab your token: `TOKEN=$(cat ~/.local/state/huginn/token)` — needed
      for every `curl` below. It changes on every daemon restart.
- [ ] Optional: keep the dashboard open at `http://127.0.0.1:47100` to watch
      cards flip state live instead of polling the API.

## Checking progress

After any step, see what fired:

```sh
TOKEN=$(cat ~/.local/state/huginn/token)
curl -s -H "X-Huginn-Token: $TOKEN" http://127.0.0.1:47100/api/hook-stats | python3 -m json.tool
```

Watch for new `codex.<Event>` keys appearing, or existing ones incrementing.
Counts persist across daemon restarts, so this can be checked days later.

---

## 1. Baseline hook coverage (issue #2)

Trigger each event at least once through normal Codex use (desktop app or
CLI, whichever you have):

- [ ] Start a brand-new Codex thread → expect `codex.SessionStart` (if Codex
      supports it — this is exactly what's unconfirmed).
- [ ] Send the first prompt in that thread → expect `codex.UserPromptSubmit`.
- [ ] Let something trivial run to completion (e.g. "list the files in this
      repo") → expect `codex.Stop`.
- [ ] Close the thread / quit the session → expect `codex.SessionEnd`.
- [ ] Leave a finished thread idle for a bit, or trigger whatever Codex's
      closest equivalent to "needs your attention" is → expect
      `codex.Notification` (unclear if this exists for Codex at all; noting
      whether it never fires is itself the answer to #2).

After a normal day of mixed use, whatever `codex.*` keys are still at zero
in `/api/hook-stats` are either not firing or not supported by this Codex
build — that's the finding to post back on #2.

## 2. Approval / waiting-state payloads (issue #3 confidence check)

Requires an approval mode that actually prompts (this machine's existing
threads all use `never`/`on-request`, which is why nothing has fired so
far):

- [ ] Set approval mode to something that forces a prompt (e.g. `untrusted`,
      or manual per-command approval) for one thread.
- [ ] Ask Codex to run a shell command that needs approval (e.g. "run
      `ls -la`"). Confirm the huginn card for that thread flips to
      `waiting_permission` when the approval prompt appears.
- [ ] Approve it, confirm the card flips back to `working`/`done`.
- [ ] Repeat for a file-edit action that needs approval (exercises
      `apply_patch_approval_request` instead of `exec_approval_request`).
- [ ] If Codex ever asks a genuine clarifying question mid-turn (not an
      approval), confirm the card shows `waiting_input`.
- [ ] While one of these prompts is up, grab the raw rollout line if you can
      (`grep -h approval ~/.codex/sessions/**/*.jsonl` or similar) and sanity
      check its field names against what `CodexAnalyzer.feed()` in
      `huginn/sources/transcript.py` expects (`call_id`, `command`, etc.) —
      this would be the first real data point for that inferred schema.

If a card *doesn't* flip to the expected waiting state, that's a real bug to
file, not just a documentation gap — the event type string or field shape
guessed in #3 was wrong.

## 3. Codex subagents (issue #8 bonus, optional)

Only relevant if Codex's spawn-subagent / bulk agent-job feature is
available to you — `thread_spawn_edges` in `~/.codex/state_5.sqlite` is
empty on this machine because that feature has never been used here.

- [ ] Kick off something that spawns a Codex subagent/child thread.
- [ ] Check the parent's huginn card for a "N subagents: ..." line (dashboard
      or `GET /api/sessions`).
- [ ] If it doesn't show up, check the table directly:
      `sqlite3 ~/.codex/state_5.sqlite "SELECT * FROM thread_spawn_edges"` —
      if rows exist but the card is blank, `_subagent_counts()` in
      `huginn/sources/codex.py` needs a look.

## 4. Notification pattern tuning (issue #1 — Claude Code, not Codex)

Different tool, same idea: let real traffic accumulate, then tune the
patterns in `huginn/config.py`'s `[patterns]` section from it.

- [ ] Turn on debug logging:
      ```sh
      curl -s -X PUT -H "X-Huginn-Token: $TOKEN" -H 'Content-Type: application/json' \
        -d '{"patterns":{"debug_log": true}}' http://127.0.0.1:47100/api/settings
      ```
- [ ] Use Claude Code normally for a few days — permission prompts,
      `AskUserQuestion`, idle turns, whatever comes up naturally.
- [ ] Periodically check `~/.local/state/huginn/notifications.log` (one JSON
      line per Notification hook: `{ts, source, message}`).
- [ ] Once there's a real corpus, compare it against
      `[patterns].permission`/`[patterns].waiting` in
      `~/.config/huginn/config.toml` (or the defaults in `huginn/config.py`)
      and tighten the lists for anything that's misclassifying. Add a
      regression case to `tests/test_reducer.py` for each new pattern.
- [ ] Turn `debug_log` back off when satisfied (same `PUT` with `false`) —
      it's opt-in and local-only, but no reason to keep it running forever.

## Wrap-up

- [ ] Post findings on the relevant issue(s) (`gh issue comment 1/2/3 ...`).
- [ ] Close **#2** if `/api/hook-stats` shows the expected `codex.*` keys
      firing (or confirms some genuinely don't — that's still a resolved
      finding, not a blocker).
- [ ] Close **#1** once the pattern lists have been tightened from real data.
- [ ] If **#3**'s field-shape guesses turn out wrong, file a follow-up issue
      (or just fix `CodexAnalyzer.feed()` directly — it's a small, isolated
      change) rather than reopening #3 itself.
