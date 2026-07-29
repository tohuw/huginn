---
type: "Knowledge Article"
title: "Event bus and reducer"
description: "One asyncio.Queue, one pure Reducer, fan-out to SSE subscribers; no per-agent subscription graph."
tags: ["architecture", "bus", "reducer", "sse"]
timestamp: "2026-07-29T00:00:00Z"
category: "architecture"
status: "current"
updated: "2026-07-29"
summary: "One asyncio.Queue, one pure Reducer, fan-out to SSE subscribers; no per-agent subscription graph."
related: ["what-huginn-is", "sources-polling-and-watching", "plugin-entry-point-contract"]
---

`huginn/bus.py`'s `Bus` is deliberately small: an `asyncio.Queue[Event]`
that every source (built-in or plugin) pushes onto via `Bus.emit(event)`,
plus a set of per-client SSE subscriber queues that `Bus.broadcast(...)`
fans a reducer result out to. A slow/disconnected dashboard client is
simply dropped from the subscriber set on a full queue — it reconnects and
re-snapshots rather than the bus blocking or buffering unbounded state for
it.

There is exactly one `Reducer` (`huginn/state.py`) consuming that queue.
Every event — a local Claude Code session state change, a Codex poll
result, a plugin's `plugin.session`/`plugin.remove` event — is folded into
one shared in-memory session-state model, which is then what gets
broadcast to SSE subscribers and served to the dashboard/CLI. There is no
per-agent subscription graph, no equivalent of one agent "receiving"
another agent's output and reacting to it — this system tracks activity and
surfaces attention-worthy state, it does not chain automations together.

This matters for anyone writing a plugin source: `SourceContext.upsert()`
and `.remove()` (see [[plugin-entry-point-contract]]) are the only way in —
a plugin never gets a handle on the `Bus` or `Reducer` directly, only a
narrow `SourceContext` scoped to its own namespace.
