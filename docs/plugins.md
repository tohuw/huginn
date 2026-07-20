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
provider to use its default.

Providers should keep credentials outside source and configuration files, bound
subprocess lifetimes and output, and return a short availability reason instead
of failing during discovery.

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
                source="example",
                session_id=external_id,
                cwd="example/project",
                name="example-session",
            )
            context.upsert(session)
            context.ok()
            await asyncio.sleep(10)
```

Use `context.key(external_id)` for every record. This gives each plugin/source a
collision-proof namespace. Call `context.remove(key)` when a previously emitted
record is definitively gone; do not infer removal from one partial poll. Report
bounded failures with `context.error(exception)` and keep retry/backoff policy in
the plugin.

## Submodules

Submodules under core's `plugins/` directory are a convenient way to pin reviewed
source revisions and develop multiple repositories together. They are not the
runtime discovery mechanism. Install a checked-out plugin into the active Huginn
environment, for example:

```sh
uv pip install -e plugins/huginn-plugin-example
```

This split lets packaged Huginn installations discover plugins normally while a
source checkout can reproduce the exact contributed plugin revisions.
