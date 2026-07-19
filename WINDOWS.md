# Windows 11 support

Windows support is under active development. Native Windows paths, process inspection,
Claude/Codex discovery, hooks, provider lifecycle, focus routing, CI, and the tray shell
are implemented. The portable package still needs validation on a real Windows 11 host;
exact Windows Terminal tab selection remains a known degradation.

## Architecture

The native shell lives in `windows/Huginn.Tray`. It is a .NET 8 WinForms `NotifyIcon` application that:

- starts and supervises the Huginn daemon without opening a console window;
- polls the authenticated localhost API and displays agents needing attention;
- opens the dashboard and asks the daemon to focus an agent;
- supports refresh, restart, quit, and per-user start-at-login;
- reads state from `%LOCALAPPDATA%\Huginn`;
- writes daemon output to `%LOCALAPPDATA%\Huginn\tray.log`.

The shell contains no session-reduction logic. Session discovery, state, authentication, and focus remain owned by the Python daemon.

## Build

Install the .NET 8 SDK, then run in PowerShell:

```powershell
.\windows\build.ps1
```

This produces `dist\windows\Huginn-win-x64.zip`. The executable is self-contained, so target machines do not need .NET installed.

To stage an already-prepared Python runtime alongside it:

```powershell
.\windows\build.ps1 -PythonRuntime C:\path\to\runtime
```

The staged runtime must contain either `python.exe` with an importable `huginn` package or `Scripts\huginn.exe`. Without a staged runtime, the tray falls back to `huginn.exe` on `PATH`.

ARM64 packaging is available for development with `-RuntimeIdentifier win-arm64`; it is not yet a supported release target.

## Development run

```powershell
dotnet run --project .\windows\Huginn.Tray\Huginn.Tray.csproj
```

For a source checkout, install Huginn into the active Windows Python environment and ensure `huginn.exe` is on `PATH`.

## Implementation status

- [x] Preserve macOS behavior behind explicit platform adapters.
- [x] Implement Windows config/state paths, process inspection, source discovery, and hooks.
- [x] Implement VS Code and Windows Terminal window focus with explicit exact-tab degradation.
- [x] Add Windows Python CI, tray compilation, and portable packaging jobs.
- [x] Add a native tray shell with daemon supervision and per-user startup registration.
- [ ] Validate the full suite and portable ZIP on `windows-latest`.
- [x] Add a normalized-session WSL bridge for Claude/Codex discovery.
- [ ] Extend the WSL bridge with transcript tails, hooks, and exact terminal focus.
- [ ] Publish a bundled Python runtime rather than relying on a staged runtime or `PATH`.
- [ ] Add an installer and promote ARM64 after the portable build is stable.

Exact Windows Terminal tab selection remains the main known risk. Window-level focus and VS Code reopening can ship first; Huginn must not silently open an unrelated terminal tab when exact focus is unavailable.
