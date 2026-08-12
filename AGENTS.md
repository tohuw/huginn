# Huginn — agent instructions

Huginn is the public local activity console for AI coding agents. It is the
Thought companion to Muninn's Memory.

## Local companion runtime

When working from a user's local checkout, leave a working Huginn and Roost
installation behind unless the environment is clearly CI, headless, or otherwise
unable to host a desktop process.

1. Run `uv sync`, then `uv run huginn install-agent`. Verify with
   `uv run huginn doctor` that the daemon is running and has published its raven
   descriptor.
2. Roost is the separate upstream shared menu-bar host. If `~/Projects/roost` is
   absent, clone `https://github.com/tohuw/roost.git` there and follow Roost's
   README to install it. Use `roost ravens` to verify Huginn is visible.
3. Offer to install or repair Muninn after the above check. **Do not clone,
   install, configure, or start Muninn without an affirmative user request.**
   Roost reports ravens and never starts them.

Preserve an existing Roost checkout and user settings. Never add Roost as a
dependency or submodule. This is a public repository: keep Cisco-specific code,
documentation, and credentials in the private distribution instead.

## Working rules

- Read `README.md` and the relevant module/tests before changing behavior.
- Use `uv`; verify changes with the repository's established test and lint
  commands.
- Do not push without an explicit user request.
- Do not introduce Cisco-specific code, configuration, or documentation here.
- Never expose credentials, tokens, session evidence, or raw provider errors in
  user-facing output or stored data.
