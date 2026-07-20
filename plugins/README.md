# Contributed Huginn plugins

These Git submodules pin reviewed source revisions for coordinated development:

- `huginn-plugin-bedrock`: Claude on Amazon Bedrock Ask/blurb provider
- `huginn-plugin-neo-cortex`: read-only Neo-Cortex worker session source

Submodules are source/version coordination, not the runtime loading mechanism.
After cloning core, initialize and install the packages into Huginn's active
environment:

```sh
git submodule update --init --recursive
uv pip install -e plugins/huginn-plugin-bedrock -e plugins/huginn-plugin-neo-cortex
huginn doctor
```

Huginn discovers the installed distributions through the `huginn.plugins`
entry-point group. Installation is the native-code trust decision; review each
pinned revision before installing or advancing a submodule.

The plugins remain independent repositories so they can version, test, and
ship without coupling AWS or Neo-Cortex dependencies to core. Advance a pin in
a focused core PR only after the plugin's offline tests, lint, and package build
pass at that revision.
