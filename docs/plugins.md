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

### Reporting data lag (optional)

`huginn doctor` reports how far each source's derived sessions trail the
artifacts they came from, so a source that quietly stops keeping up is visible
rather than discovered later. A source that reads a filesystem can opt in with
one synchronous method:

```python
    def artifact_mtime(self) -> float | None:
        """Newest epoch mtime this source would consume right now."""
        return newest_upstream_write() or None
```

Return the newest timestamp among the artifacts currently worth processing, or
`None` when there is nothing to process or the answer is unknown — doctor
treats unknown as unknown, never as stale. Omit the method entirely if the
source does not watch files; doctor then reports it as not measuring artifact
times. The call must be quick and must not raise; an exception is reported as
unknown lag rather than failing the report.

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
