# Windows 11 support

Windows support is under active development. Native Windows paths, process
inspection, Claude/Codex discovery, hooks, provider lifecycle, focus routing, and
CI are implemented. Exact Windows Terminal tab selection remains a known
degradation.

## The tray shell was removed

There used to be a .NET 8 WinForms `NotifyIcon` shell in `windows/Huginn.Tray`
that supervised the daemon, polled the API, and offered refresh/restart/quit plus
its own start-at-login toggle. It is gone, along with `windows/build.ps1` and the
CI steps that compiled and packaged it.

Its replacement is **[Roost](https://github.com/tohuw/roost)**, one shared status
menu bar (a macOS menu bar item and a Windows system tray item) that shows every
running *raven* rather than one per tool. Huginn publishes a descriptor and serves
a menu; Roost renders it. See "Shared status menu bar" in
[README.md](README.md#shared-status-menu-bar) for the wire format, and
`huginn/raven.py` for Huginn's side.

**What changed for a Windows user, concretely:**

| | Old tray | Roost |
|---|---|---|
| Shows | Huginn only | every running raven in one tray item |
| Install | a self-contained ZIP with a staged Python runtime | its own repository, `python -m roost.cli install` |
| Quit / Restart the daemon | tray menu items | rows Huginn publishes in the shared menu, handled by `huginn/raven.py` |
| Start a stopped daemon | the tray launched it | `huginn serve`, or `huginn install-agent` for start-at-login |
| Start at login | the tray's own `Huginn` `Run` value | `huginn install-agent` (`HuginnDaemon`), or Roost's own startup entry for the tray |

The one genuine loss is **click-to-start**: nothing in the menu bar launches a
stopped daemon any more. That is deliberate rather than an omission. A stopped
daemon has withdrawn its descriptor, so a shared menu bar cannot see it to offer a
row, and the alternative — a file naming an interpreter for the tray to execute —
is the write-then-execute path issue #41 M5 hardened `daemon.json` against, made
worse by living in one process shared across every raven. Start-at-login is the
supervisor's job, which on Windows is the `Run` key below.

## Start at login

```powershell
huginn install-agent      # huginn uninstall-agent to remove
```

This registers a console-free `HuginnDaemon` value under
`HKCU\Software\Microsoft\Windows\CurrentVersion\Run` (pythonw.exe, so no console
window appears at every login). The `Run` key starts the daemon once per login and
does not restart it if it exits — which now matches the behaviour on the other
platforms more closely than it did, because nothing is supervising it.

A Scheduled Task was rejected when this was built: it would have added a third
owner of the daemon's lifecycle, with a separate credential and trigger surface,
to deliver what one registry value does. That reasoning still holds.

**A note on the tray guard in the code.** `install-agent` still refuses to install
while a `Huginn` value (the old tray's own startup entry) is present under the same
`Run` key, and `huginn/agent_install.py` still declares `TRAY_RUN_VALUE`. That is
kept on purpose, not overlooked: a machine that ran the old portable tray still has
that registry value, and installing `HuginnDaemon` beside it would produce two
autostarts and resurrect a daemon the user just quit — the exact conflict the guard
was written for. It refuses with an explanation rather than silently competing.
Remove the stale `Huginn` value (or uninstall the old tray) and the install
proceeds. Roost registers its own startup entry under its own name and does not
collide with either.

## Implementation status

- [x] Preserve macOS behavior behind explicit platform adapters.
- [x] Implement Windows config/state paths, process inspection, source discovery, and hooks.
- [x] Implement VS Code and Windows Terminal window focus with explicit exact-tab degradation.
- [x] Add Windows Python CI.
- [x] Add a headless `install-agent` startup path.
- [x] Publish a raven descriptor and menu so one shared tray can show Huginn.
- [x] Retire the bespoke .NET tray in favour of Roost.
- [ ] Validate the full suite on `windows-latest`.
- [x] Add a normalized-session WSL bridge for Claude/Codex discovery.
- [ ] Extend the WSL bridge with transcript tails, hooks, and exact terminal focus.

Exact Windows Terminal tab selection remains the main known risk. Window-level
focus and VS Code reopening can ship first; Huginn must not silently open an
unrelated terminal tab when exact focus is unavailable.

Steering and Ctrl-C therefore fail closed on Windows. Observe, inspect, and
window focus remain available; foreground-window keystroke synthesis is not an
acceptable substitute for a verified exact-tab target.

Note that `huginn/platform/windows.py` is process and window inspection — it is
what makes focus and liveness work — and is unrelated to the removed tray.
