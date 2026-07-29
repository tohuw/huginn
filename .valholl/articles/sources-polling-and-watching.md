---
type: "Knowledge Article"
title: "Sources, polling, and watching"
description: "Built-in sources are a mix of fsevents watchers and pollers; each one is its own asyncio task feeding the shared bus."
tags: ["architecture", "sources", "polling", "daemon"]
timestamp: "2026-07-29T00:00:00Z"
category: "architecture"
status: "current"
updated: "2026-07-29"
summary: "Built-in sources are a mix of fsevents watchers and pollers; each one is its own asyncio task feeding the shared bus."
related: ["what-huginn-is", "event-bus-and-reducer", "plugin-entry-point-contract"]
---

There is no single "polling interval" that governs the whole daemon. Each
source under `huginn/sources/` (`claude_code.py`, `codex.py`,
`chatgpt_desktop.py`, `claude_desktop.py`, `wsl.py`, `transcript.py`) is
either a filesystem watcher (fsevents-based, reacting to transcript file
changes as they happen) or a poller with its own cadence, and the daemon
(`huginn/daemon.py`) runs each one as its own `asyncio.create_task`,
cancelled together on shutdown. Cadences for the pollers are configured in
`~/.config/huginn/config.toml`, not hardcoded globally.

An externally-installed plugin source follows the same shape but owns its
own loop entirely: the `SessionSource.run(context)` coroutine is expected
to loop forever internally (poll, sleep, repeat) rather than being called
on a schedule by the daemon. The daemon's only job is to start that
coroutine as a task and cancel it on shutdown — cadence, backoff, and error
handling are the plugin's responsibility. See
[[plugin-entry-point-contract]] for the exact contract, and note that
`SourceContext.error(exc)` / `.ok()` exist specifically so a source can
report its own health without the daemon needing to inspect its internals.

Whatever a source discovers (a session appearing, changing state, or
disappearing) becomes an `Event` pushed onto the shared bus — see
[[event-bus-and-reducer]] for what happens next. Sources never talk to each
other directly and never share state; the bus and the single `Reducer` are
the only integration point.
