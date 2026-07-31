# Huginn plugins

Huginn plugins are trusted Python distributions. Installing one grants it the
same local-user permissions as Huginn, including whatever transcript and process
metadata the daemon can read. Review plugin source and provenance before
installation.

## Discovery

Register one package-metadata entry point in `pyproject.toml`:

```toml
[project.entry-points."huginn.plugins"]
example = "huginn_example:plugin"
```

The referenced object is either a `PluginSpec` or a zero-argument factory that
returns one:

```python
from huginn.plugins import API_VERSION, PluginSpec

plugin = PluginSpec(
    name="example",
    version="1.0.0",
    api_version=API_VERSION,
    providers=(ExampleProvider(),),
    sources=(ExampleSource(),),
)
```

Huginn loads entry points in name order, isolates load failures, rejects duplicate
or malformed capability names, and reserves the built-in provider names `claude`
and `codex`. `huginn doctor` and authenticated `GET /api/plugins` report the
result. Arbitrary directories are never scanned or imported.

## API version ranges

Core advertises the inclusive range `MIN_API_VERSION..API_VERSION`. A plugin is
loaded when its own supported range overlaps core's. Declare a range with
`min_api`/`max_api` so a routine core `API_VERSION` bump does not disable your
plugin:

```python
plugin = PluginSpec(
    name="example",
    version="1.0.0",
    api_version=API_VERSION,
    min_api=1,
    max_api=2,          # keeps loading after core bumps to API 2
    providers=(ExampleProvider(),),
)
```

Both default to `api_version`, so a spec that sets only `api_version` behaves
exactly as before: accepted when the versions match, refused when they do not.
Declare the widest range you have actually tested — a range is a compatibility
claim, not a wish.

A range that does not overlap core's is reported loudly rather than skipped
quietly: a `WARNING` on the daemon log at every start naming both ranges, a
labelled `huginn doctor` error that fails the run, and an `api_mismatch: true`
error entry in `GET /api/plugins`. The plugin stays installed but contributes
nothing, which is precisely why it has to be visible.

## Model policy

A plugin registry is purely additive — plugins contribute capabilities and one
cannot veto another's — so "only these models may be used" is not expressible
as a plugin. That restriction lives in a separate chokepoint, `huginn.policy`,
declared in package metadata under its own entry-point group:

```toml
[project.entry-points."huginn.policy"]
restricted = "my_distribution.policy:APPROVED_ONLY"
```

```python
from huginn.policy import ModelPolicy

APPROVED_ONLY = ModelPolicy(
    name="approved-only",
    allow=(r"^us\.anthropic\.",),        # regex allowlist of model ids
    require_provider="bedrock",           # None = any provider
    reason="inference must route through the approved provider",
)
```

The restriction is a property of what is *installed*, not of what plugin code
chooses to register. Three rules:

- **Policies intersect, never union.** A call is permitted only when every
  resolved policy permits it. Installing a second, permissive policy cannot
  restore what a restrictive one removed. Config, environment, and CLI input
  may narrow the allowed set but never widen it — `PUT /api/settings` and Ask's
  "use codex" control both refuse a value an installed policy forbids.
- **Fail closed.** A (model, provider) pair no policy addresses is refused, and
  the policy's `reason` is shown verbatim. A refused model is never silently
  swapped for a permitted one: Ask returns the reason, automatic text stops,
  and `GET /api/providers` reports `available: false` with that reason.
- **A policy that fails to load refuses everything.** Unlike a broken plugin,
  which is skipped, a broken policy becomes a synthetic policy allowing
  nothing. Dropping it would widen the permitted set, which is the one outcome
  this exists to prevent.

`allow` patterns use `re.search`, so anchor with `^` for a strict prefix match.
With no policy installed, `huginn doctor` reports "none (every model
permitted)" and behaviour is unchanged — the permissive default is itself a
real `ModelPolicy`, so restricted and unrestricted builds run the same code.

### Honest scope

This is a strong contract, not a sandbox. It governs Huginn's own calls. Anyone
with write access to the environment can edit anything, and nothing here stops
a user running any model in another tool. Its value is being explicit,
testable, and CI-verifiable: it prevents accidental violation and makes drift a
build failure rather than a silent capability.

One consequence worth planning for: **under a restrictive policy a distribution
cannot shell out to a vendor CLI** (for example `claude -p`, which is how
Huginn's own built-in providers work) for generation. That subprocess inherits
the user's own configuration and can egress to an endpoint the policy forbids,
entirely outside Huginn's control. A restricted distribution must contribute a
provider that calls the approved API directly — which also means enrichment
incurs API spend rather than riding a subscription.

## Ask providers

A provider has a lowercase `name`, `available()`, `run_text()`, and asynchronous
`stream()` with the same keyword contract as the built-in providers. An optional
`label` is shown in the dashboard. An optional `compatible_model(value)` method
can accept or reject a configured model name; returning an empty string asks the
provider to use its default. Providers whose automatic-title model identifiers
differ from Claude's may implement `resolve_blurb_model(configured)` to map a
generic Haiku preference to their own backend identifier.

Providers should keep credentials outside source and configuration files, bound
subprocess lifetimes and output, and return a short availability reason instead
of failing during discovery. Provider exceptions may carry a boolean
`retryable` attribute (or use `LLMProviderError`) so automatic work can
circuit-break permanent configuration/authentication failures without parsing
message text.

## Session sources

A source has a lowercase `name` and one long-running coroutine:

```python
import asyncio

from huginn.model import Session


class ExampleSource:
    name = "sessions"

    async def run(self, context):
        while True:
            external_id = "stable-upstream-id"
            session = Session(
                key=context.key(external_id),
                source=self.name,
                session_id=external_id,
                cwd="example/project",
                name="example-session",
                source_summary="status: waiting\nmessage: review requested",
            )
            context.upsert(session)
            context.ok()
            await asyncio.sleep(10)
```

Use `context.key(external_id)` for every record. This gives each plugin/source a
collision-proof namespace. Call `context.remove(key)` when a previously emitted
record is definitively gone; do not infer removal from one partial poll. Report
bounded failures with `context.error(exception)` and keep retry/backoff policy in
the plugin. A source may put up to 4,000 characters of authoritative current
evidence in `Session.source_summary`; Peek and Ask use it when there is no local
transcript. Keep it factual, current, and free of credentials. The session's
`source` must equal the source capability's `name`.

At startup, `context.existing_keys()` returns only keys in this exact
plugin/source namespace, including records restored from Huginn's private
snapshot. Seed reconciliation state from those keys so a record removed while
Huginn was stopped can age out after successful upstream polls. No other
source's keys or session contents are exposed through this capability.

## Dashboard session groups

Set `Session.group` (and, optionally, `Session.group_label`) to have every
session your source contributes render in its own dashboard section instead
of the main grid, with one collective show/hide toggle -- the same treatment
built-in desktop-app tiles get, generalized for plugins:

```python
session = Session(
    key=context.key(external_id),
    source=self.name,
    session_id=external_id,
    cwd="example/project",
    name="example-session",
    group="example",                     # short, stable key
    group_label="Example workers",       # human-readable section heading
)
```

`group` must match the same name pattern as a plugin/provider/source name
(lowercase, `[a-z][a-z0-9._-]*`). `group_label` is optional -- it falls back
to `group` itself -- but if you set it, keep it under 60 characters and
non-empty. Every session sharing a `group` key renders together; the
dashboard creates that section the first time a session declares it, and
hides it entirely once no live session claims it. The toggle state persists
per-browser through the existing settings sync (`ui.hidden_groups`), same as
every other dashboard control. Leaving both fields unset (the default)
renders sessions in the main grid exactly as before this existed.

## Installing a plugin

Install a plugin package into Huginn's active environment, for example:

```sh
uv pip install -e /path/to/huginn-plugin-example
```

Huginn discovers it through the `huginn.plugins` entry point at the next start.
