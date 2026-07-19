# Fixture provenance

Real, redacted samples captured from a live install, not hand-written --
issue #22. Hand-written minimal dicts pass even after upstream field
nesting changes; these don't, because they carry the real shape.

| Fixture | Captured from | Version | Date |
|---|---|---|---|
| `claude_transcript_lines.jsonl` | `~/.claude/projects/*.jsonl` | Claude Code 2.1.214 | 2026-07-19 |
| `codex_rollout_lines.jsonl` | `~/.codex/sessions/**/*.jsonl` | Codex CLI 0.144.6 | 2026-07-19 (completion sample refreshed 2026-07-19) |
| `codex_state_db_schema.sql` | `~/.codex/state_5.sqlite` (`.schema`) | Codex CLI 0.144.6 | 2026-07-19 |

## Redaction

Every fixture had prompts, file paths, session/turn/request ids, and token
counts replaced with fixed placeholders before being checked in. Field
*names* and *nesting* are real; field *values* are not, except for a few
structural constants a parser actually branches on (`type`, `status`,
tool/event names) and the version strings above.

`codex_state_db_schema.sql` is schema only (`sqlite3 ... ".schema <table>"`)
-- no rows, so nothing to redact there.

## Refreshing fixtures

When upgrading Claude Code or Codex, or after either changes payload shape
in a way a test starts failing against:

1. Find a real, recent example of the entry type you need:
   - Claude: `grep -l '"type":"<kind>"' ~/.claude/projects/*/*.jsonl`
   - Codex: `grep -l '"type":"<kind>"' ~/.codex/sessions/**/*.jsonl`
2. Extract that one JSON line and redact it by hand (or adapt the
   throwaway script used to build these -- walk the dict, blank
   `message`/`content`/`text` string values, zero out token-count ints,
   replace `cwd`/path fields with `/redacted/...`, replace
   `uuid`/`sessionId`/`turn_id`-shaped fields with a fixed placeholder
   UUID). Keep every key name and the overall nesting untouched --
   that structure is the entire point.
3. Update the version/date in the provenance table above.
4. For the sqlite schema: `sqlite3 ~/.codex/state_5.sqlite ".schema <table>"`
   -- no redaction needed, it's DDL only.
5. Bump `huginn/doctor.py`'s `TESTED_CLAUDE_VERSION`/`TESTED_CODEX_VERSION`
   to match, so `huginn doctor` stops warning about running ahead of
   fixture coverage.
