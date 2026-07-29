---
type: "Knowledge Article"
title: "What Huginn is"
description: "A local AI agent activity console (sources -> bus -> reducer -> SSE -> dashboard), not the Rails/RSS agent-automation project of the same name."
tags: ["architecture", "overview"]
timestamp: "2026-07-29T00:00:00Z"
category: "architecture"
status: "current"
updated: "2026-07-29"
summary: "A local AI agent activity console (sources -> bus -> reducer -> SSE -> dashboard), not the Rails/RSS agent-automation project of the same name."
related: ["sources-polling-and-watching", "event-bus-and-reducer", "plugin-entry-point-contract"]
---

This repository shares its name with an unrelated, older Ruby/Rails project
(cantino/huginn, an RSS/webhook/IFTTT-style automation platform with a
Rails `Agent` model, `check`/`receive` lifecycle methods, and a `whenever`/
Sidekiq-driven scheduler). This is a different codebase with a different
architecture. If you came here expecting `app/models/agent.rb`, `Event`,
`create_event`, or an `ADDITIONAL_GEMS` Gemfile plugin mechanism, none of
that exists in this repository.

What this project actually is: a local, Python (`uv`-managed) daemon and web
dashboard that watches AI coding-agent activity on your own machine —
Claude Code and Codex terminal sessions (first-class), plus app-level
presence for ChatGPT Desktop and Claude Desktop — and surfaces what needs
your attention (permission requests, blocked input, errors) in one place
instead of tabbing between terminals. Everything runs locally; no session
data leaves the machine except the agent CLI calls you already make
yourself.

The high-level data flow, as stated in the project's own README, is:

```
sources (fsevents watchers + pollers) -> event bus -> one reducer -> SSE -> dashboard
```

See [[sources-polling-and-watching]] for how individual sources work,
[[event-bus-and-reducer]] for the middle of that pipeline, and
[[plugin-entry-point-contract]] for how an external package (for example,
a Cisco-specific distribution wrapping this repo) adds a new provider or
source without forking this code.
