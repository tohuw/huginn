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

**A pythonw process has no standard streams**, so `sys.stdout` and `sys.stderr`
are `None`. Uvicorn's default log config calls `sys.stdout.isatty()` while
building its formatter, which made the daemon die on startup — before binding
its port — with `AttributeError: 'NoneType' object has no attribute 'isatty'`.
Start-at-login therefore never worked on Windows, while starting the daemon by
hand from a terminal always did, which is what kept it hidden. `cli.main` now
binds missing streams to devnull before anything can touch them.

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

## Why jump did nothing

Two independent defects, both of which made the jump button look inert.

**The foreground lock.** Windows only lets the process that currently owns the
foreground hand it to someone else. The daemon is by definition not that
process, so its `SetForegroundWindow` was simply refused — it returns 0, nothing
moves, and the only visible effect is a taskbar button flashing. Attaching to
the foreground thread's input queue (`AttachThreadInput`) for the duration of
the call is the documented way through, and it is what makes the call actually
take effect. Measured on this machine: refused before, granted after.

**The wrong window.** Focus used to merge the session's process ancestry with
*every* `WindowsTerminal.exe` pid into one candidate set, then take the first
window `EnumWindows` returned — which enumerates in Z-order, so it picked
whichever terminal was topmost rather than the one hosting the session. A shell
under Windows Terminal does reach `WindowsTerminal.exe` by walking parents
(`pwsh` → `claude` → `pwsh` → `WindowsTerminal`), so the ancestry is searched
first and on its own. The all-terminals search remains only as a fallback for a
session whose ancestry is broken, where one window still beats none.

Exact tab selection is still unavailable, so the result says so
(`exact tab unavailable`): with several sessions in tabs of one window, jump
brings the right window forward but leaves the active tab alone.

## Implementation status

- [x] Preserve macOS behavior behind explicit platform adapters.
- [x] Implement Windows config/state paths, process inspection, source discovery, and hooks.
- [x] Implement VS Code and Windows Terminal window focus with explicit exact-tab degradation.
- [x] Add Windows Python CI.
- [x] Add a headless `install-agent` startup path.
- [x] Publish a raven descriptor and menu so one shared tray can show Huginn.
- [x] Retire the bespoke .NET tray in favour of Roost.
- [ ] Validate the full suite on `windows-latest`.
- [x] Make jump work from the daemon: foreground lock, and the session's own window.
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
