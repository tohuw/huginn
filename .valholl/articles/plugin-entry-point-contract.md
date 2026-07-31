---
type: "Knowledge Article"
title: "Plugin entry-point contract"
description: "How an external Python distribution registers an LLMProvider or SessionSource under the huginn.plugins entry-point group, and what the daemon guarantees/enforces at load time."
tags: ["plugins", "extensibility", "entry-points"]
timestamp: "2026-07-29T00:00:00Z"
category: "extensibility"
status: "current"
updated: "2026-07-31"
summary: "How an external Python distribution registers an LLMProvider or SessionSource under the huginn.plugins entry-point group, and what the daemon guarantees/enforces at load time. Plugin API compatibility is a declared range, not an exact match."
related: ["what-huginn-is", "event-bus-and-reducer", "sources-polling-and-watching"]
---

Huginn's only extension point is standard Python package-metadata entry
points, group `huginn.plugins` (`huginn/plugins.py`). It never scans
directories or paths for plugin code — installing the distribution into the
same environment is the trust decision. A distribution registers by pointing
an entry point at a module-level object (or zero-arg factory) that resolves
to a `PluginSpec`:

```toml
[project.entry-points."huginn.plugins"]
my-plugin = "my_package:plugin"
```

`PluginSpec(name, version, api_version, providers=(), sources=())` — a
plugin can contribute providers only, sources only, or both. There are two
capability protocols, both structural (`typing.Protocol`, no inheritance
required):

- `LLMProvider` — `name`, `available() -> str | None`, async `run_text(...)
  -> str`, `stream(...)` (an async generator). Consumed by the daemon
  wherever it dispatches an Ask/chat request; provider names `claude` and
  `codex` are reserved for the built-ins and cannot be registered by a
  plugin.
- `SessionSource` — `name`, async `run(self, context: SourceContext) ->
  None`. The daemon starts this coroutine as its own `asyncio.create_task`
  and expects it to loop internally for the life of the process (see
  [[sources-polling-and-watching]]).

`discover_plugins()` loads entry points in name order and isolates
failures per-plugin: a broken distribution becomes a `PluginLoadError`
(visible via `huginn doctor` and the plugins API) rather than crashing the
daemon or blocking other plugins from loading. Validation enforced at load
time (`_validate_plugin`): the plugin's supported API range must overlap
core's; plugin, provider, and source names must match a lowercase
dot/dash/underscore pattern; provider and source names must be unique within
the plugin and across all loaded plugins.

Core advertises the inclusive range `MIN_API_VERSION..API_VERSION`. A plugin
declares its own with `min_api`/`max_api`, both defaulting to `api_version`,
so a spec setting only `api_version` is accepted exactly when the versions
match — the pre-range behaviour, preserved. This replaced an exact
`api_version != API_VERSION` comparison whose failure mode (issue #38) was
that a routine core bump silently disabled every installed plugin: they
stayed *installed* and stopped *existing* from the daemon's perspective, with
no crash, no log line, and nothing but a `huginn doctor` line to show it. A
non-overlapping range is now reported as a mismatch specifically — a
`logging.WARNING` naming both ranges at every daemon start, a labelled
`doctor` error that fails the run, and `api_mismatch: true` on the
`GET /api/plugins` error entry — because the usual cause affects every plugin
at once and the actionable fact is which side has to move.

The registry is `@lru_cache`-memoized for the life of the process
(`get_registry()`) — there is no live reload. A newly installed or fixed
plugin package requires a daemon restart before it's picked up.

A `SessionSource.run()` never gets the raw `Bus`/`Config`/`Diagnostics` —
only a `SourceContext` scoped to `f"{plugin_name}.{source_name}"`:
`key(external_id)` builds a collision-proof, namespace-prefixed session
key; `existing_keys()` returns only this source's own prior keys (for
reconciling after a restart); `upsert(session)` and `remove(key)` are the
only way to publish state, and both reject a session/key that doesn't
belong to the calling source's namespace; `ok()`/`error(exc)` report health
into the daemon's diagnostics under that same namespace. This means one
plugin cannot forge another plugin's session keys or read/write outside
its own namespace, and payload shape limits (string lengths, non-empty
labels) are enforced centrally rather than trusted to each plugin's own
code.
