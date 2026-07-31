---
okf_version: "0.1"
---

# Huginn wiki

## architecture

* [What Huginn is](articles/what-huginn-is.md) - A local AI agent activity console (sources → bus → reducer → SSE → dashboard), not the Rails/RSS agent-automation project of the same name. (Status: current; updated: 2026-07-29.)
* [Sources, polling, and watching](articles/sources-polling-and-watching.md) - Built-in sources are a mix of fsevents watchers and pollers; each one is its own asyncio task feeding the shared bus. (Status: current; updated: 2026-07-29.)
* [Event bus and reducer](articles/event-bus-and-reducer.md) - One `asyncio.Queue`, one pure `Reducer`, fan-out to SSE subscribers; no per-agent subscription graph. (Status: current; updated: 2026-07-29.)

## extensibility

* [Plugin entry-point contract](articles/plugin-entry-point-contract.md) - How an external Python distribution registers an `LLMProvider` or `SessionSource` under the `huginn.plugins` entry-point group, and what the daemon guarantees/enforces at load time. Plugin API compatibility is a declared range, not an exact match; because the registry is additive, model restrictions live in a separate intersecting policy chokepoint. (Status: current; updated: 2026-07-31.)
