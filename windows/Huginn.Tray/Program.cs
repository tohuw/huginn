using System.Diagnostics;
using System.Net.Http.Json;
using System.Text.Json.Serialization;
using Microsoft.Win32;

namespace Huginn.Tray;

internal static class Program
{
    private const string MutexName = @"Local\Huginn.Tray";

    [STAThread]
    private static void Main()
    {
        using var singleInstance = new Mutex(initiallyOwned: true, name: MutexName, createdNew: out var createdNew);
        if (!createdNew) return;
        ApplicationConfiguration.Initialize();
        Application.Run(new TrayContext());
    }
}

internal sealed class TrayContext : ApplicationContext
{
    private const string RunKey = @"Software\Microsoft\Windows\CurrentVersion\Run";
    private const string RunValue = "Huginn";
    private static readonly string StateDirectory = ResolveStateDirectory();

    private readonly NotifyIcon tray = new() { Icon = SystemIcons.Application, Visible = true, Text = "Huginn" };
    private readonly HttpClient http = new() { Timeout = TimeSpan.FromSeconds(2) };
    private readonly System.Windows.Forms.Timer timer = new() { Interval = 3000 };
    private readonly Control dispatcher = new();
    private Process? daemon;
    private SessionEnvelope? roster;
    private string? lastError;
    private bool quitting;
    private bool refreshInProgress;
    private readonly object daemonLock = new();
    private readonly object logLock = new();

    public TrayContext()
    {
        // Give background Process events a stable WinForms handle to marshal to.
        dispatcher.CreateControl();
        tray.MouseDoubleClick += (_, _) => OpenConsole();
        tray.ContextMenuStrip = BuildMenu();
        timer.Tick += async (_, _) => await RefreshAsync();
        timer.Start();
        EnsureDaemon();
        _ = RefreshAfterStartupAsync();
    }

    private static string ResolveStateDirectory()
    {
        var local = Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
        return Path.Combine(local, "Huginn");
    }

    private async Task RefreshAfterStartupAsync()
    {
        await Task.Delay(800);
        await RefreshAsync();
    }

    private ContextMenuStrip BuildMenu()
    {
        var menu = new ContextMenuStrip();
        menu.Opening += (_, _) => PopulateMenu(menu);
        PopulateMenu(menu);
        return menu;
    }

    private void PopulateMenu(ContextMenuStrip menu)
    {
        menu.Items.Clear();
        var attention = roster?.Sessions.Where(s => IsAttention(s.State)).ToArray() ?? [];
        if (lastError is not null)
            menu.Items.Add(lastError, null, (_, _) => RestartDaemon());
        else if (attention.Length == 0)
            menu.Items.Add(new ToolStripMenuItem("No agents need attention") { Enabled = false });
        else
        {
            menu.Items.Add(new ToolStripMenuItem("Needs attention") { Enabled = false });
            foreach (var session in attention)
            {
                var marker = session.State switch
                {
                    "waiting_permission" => "Approve",
                    "error" => "Error",
                    _ => "Reply"
                };
                var item = new ToolStripMenuItem($"{marker}: {session.Name}")
                {
                    ToolTipText = string.IsNullOrWhiteSpace(session.LastPrompt) ? session.Cwd : session.LastPrompt
                };
                item.Click += async (_, _) => await FocusAsync(session.Key);
                menu.Items.Add(item);
            }
        }

        menu.Items.Add(new ToolStripSeparator());
        menu.Items.Add("Open Console", null, (_, _) => OpenConsole());
        menu.Items.Add("Refresh", null, async (_, _) => await RefreshAsync());
        var startup = new ToolStripMenuItem("Start at login") { Checked = StartsAtLogin(), CheckOnClick = true };
        startup.CheckedChanged += (_, _) =>
        {
            try { SetStartAtLogin(startup.Checked); }
            catch (Exception ex) when (ex is UnauthorizedAccessException or System.Security.SecurityException)
            {
                lastError = "Could not update start-at-login setting";
            }
        };
        menu.Items.Add(startup);
        menu.Items.Add(new ToolStripSeparator());
        menu.Items.Add("Restart Huginn", null, (_, _) => RestartDaemon());
        menu.Items.Add("Quit Huginn", null, (_, _) => Quit());
    }

    private static bool IsAttention(string state) =>
        state is "waiting_permission" or "waiting_input" or "error";

    private async Task RefreshAsync()
    {
        if (refreshInProgress || quitting) return;
        refreshInProgress = true;
        try
        {
            var (port, token) = ReadConnection();
            using var request = new HttpRequestMessage(HttpMethod.Get, $"http://127.0.0.1:{port}/api/sessions");
            request.Headers.Add("X-Huginn-Token", token);
            using var response = await http.SendAsync(request);
            response.EnsureSuccessStatusCode();
            roster = await response.Content.ReadFromJsonAsync<SessionEnvelope>();
            lastError = null;
            var count = roster?.Attention ?? 0;
            tray.Text = count == 0 ? "Huginn" : $"Huginn — {count} need attention";
        }
        catch
        {
            lastError = "Daemon unavailable — click to restart";
            EnsureDaemon();
        }
        finally
        {
            refreshInProgress = false;
        }
    }

    private static (string Port, string Token) ReadConnection() =>
        (File.ReadAllText(Path.Combine(StateDirectory, "port")).Trim(),
         File.ReadAllText(Path.Combine(StateDirectory, "token")).Trim());

    private void EnsureDaemon()
    {
        lock (daemonLock)
        {
            if (quitting || daemon is { HasExited: false } || LiveDaemonPid() is not null) return;
            Directory.CreateDirectory(StateDirectory);
            var (file, arguments, workingDirectory) = ResolveDaemonCommand();
            var log = Path.Combine(StateDirectory, "tray.log");
            var start = new ProcessStartInfo(file, arguments)
            {
                WorkingDirectory = workingDirectory,
                UseShellExecute = false,
                CreateNoWindow = true,
                WindowStyle = ProcessWindowStyle.Hidden,
                RedirectStandardOutput = true,
                RedirectStandardError = true
            };
            try
            {
                daemon = Process.Start(start);
                if (daemon is null) throw new InvalidOperationException("Process.Start returned null");
                daemon.OutputDataReceived += (_, e) => AppendLog(log, e.Data);
                daemon.ErrorDataReceived += (_, e) => AppendLog(log, e.Data);
                daemon.BeginOutputReadLine();
                daemon.BeginErrorReadLine();
                daemon.EnableRaisingEvents = true;
                daemon.Exited += (_, _) => PostToUi(() =>
                {
                    if (!quitting) lastError = "Daemon stopped — click to restart";
                });
            }
            catch (Exception ex)
            {
                daemon?.Dispose();
                daemon = null;
                lastError = $"Could not start daemon ({ex.GetType().Name})";
            }
        }
    }

    private void PostToUi(Action action)
    {
        if (quitting) return;
        try { dispatcher.BeginInvoke(action); }
        catch (InvalidOperationException) { }
    }

    private static (string File, string Arguments, string WorkingDirectory) ResolveDaemonCommand()
    {
        var baseDir = AppContext.BaseDirectory;
        var bundledPython = Path.Combine(baseDir, "runtime", "python.exe");
        if (File.Exists(bundledPython)) return (bundledPython, "-m huginn.cli serve --no-open", baseDir);
        var bundledHuginn = Path.Combine(baseDir, "runtime", "Scripts", "huginn.exe");
        if (File.Exists(bundledHuginn)) return (bundledHuginn, "serve --no-open", baseDir);
        return ("huginn.exe", "serve --no-open", baseDir);
    }

    private void AppendLog(string path, string? line)
    {
        if (line is null) return;
        try
        {
            lock (logLock)
                File.AppendAllText(path, $"{DateTimeOffset.Now:O} {line}{Environment.NewLine}");
        }
        catch (IOException) { }
        catch (UnauthorizedAccessException) { }
    }

    private static int? LiveDaemonPid()
    {
        try
        {
            var json = File.ReadAllText(Path.Combine(StateDirectory, "daemon.json"));
            var state = System.Text.Json.JsonSerializer.Deserialize<DaemonState>(json);
            if (state is null) return null;
            using var process = Process.GetProcessById(state.Pid);
            if (process.HasExited) return null;
            // Do not trust a stale daemon.json after Windows has reused its PID.
            var actualStart = new DateTimeOffset(process.StartTime.ToUniversalTime()).ToUnixTimeSeconds();
            return Math.Abs(actualStart - state.Started) <= 2 ? state.Pid : null;
        }
        catch { return null; }
    }

    private async Task FocusAsync(string key)
    {
        try
        {
            var (port, token) = ReadConnection();
            var escaped = Uri.EscapeDataString(key);
            using var request = new HttpRequestMessage(HttpMethod.Post,
                $"http://127.0.0.1:{port}/api/sessions/{escaped}/focus");
            request.Headers.Add("X-Huginn-Token", token);
            using var response = await http.SendAsync(request);
            response.EnsureSuccessStatusCode();
        }
        catch { lastError = "Could not focus agent"; }
    }

    private static void OpenConsole()
    {
        try
        {
            var (port, token) = ReadConnection();
            Process.Start(new ProcessStartInfo($"http://127.0.0.1:{port}/#t={Uri.EscapeDataString(token)}")
                { UseShellExecute = true });
        }
        catch { }
    }

    private void StopDaemon()
    {
        lock (daemonLock)
        {
            var pid = LiveDaemonPid();
            if (pid is null && daemon is { HasExited: false })
                pid = daemon.Id; // Covers shutdown before daemon.json is written.
            if (pid is not null)
            {
                try
                {
                    using var process = Process.GetProcessById(pid.Value);
                    process.Kill(entireProcessTree: true);
                    process.WaitForExit(2000);
                }
                catch { }
            }
            daemon?.Dispose();
            daemon = null;
        }
    }

    private void RestartDaemon()
    {
        StopDaemon();
        lastError = null;
        EnsureDaemon();
        _ = RefreshAfterStartupAsync();
    }

    private static bool StartsAtLogin()
    {
        try
        {
            using var key = Registry.CurrentUser.OpenSubKey(RunKey);
            return key?.GetValue(RunValue) is string;
        }
        catch (Exception ex) when (ex is UnauthorizedAccessException or System.Security.SecurityException)
        {
            return false;
        }
    }

    private static void SetStartAtLogin(bool enabled)
    {
        var executable = Environment.ProcessPath
            ?? throw new InvalidOperationException("Could not determine Huginn executable path");
        using var key = Registry.CurrentUser.CreateSubKey(RunKey)
            ?? throw new InvalidOperationException("Could not open the Windows startup registry key");
        if (enabled)
            key.SetValue(RunValue, $"\"{executable}\"", RegistryValueKind.String);
        else
            key.DeleteValue(RunValue, throwOnMissingValue: false);
    }

    private void Quit()
    {
        quitting = true;
        timer.Stop();
        StopDaemon();
        tray.Visible = false;
        tray.Dispose();
        ExitThread();
    }

    protected override void Dispose(bool disposing)
    {
        if (disposing)
        {
            timer.Dispose();
            dispatcher.Dispose();
            tray.Dispose();
            http.Dispose();
            daemon?.Dispose();
        }
        base.Dispose(disposing);
    }
}

internal sealed record SessionEnvelope(
    [property: JsonPropertyName("sessions")] AgentSession[] Sessions,
    [property: JsonPropertyName("attention")] int Attention);

internal sealed record AgentSession(
    [property: JsonPropertyName("key")] string Key,
    [property: JsonPropertyName("name")] string Name,
    [property: JsonPropertyName("state")] string State,
    [property: JsonPropertyName("cwd")] string? Cwd,
    [property: JsonPropertyName("last_prompt")] string? LastPrompt);

internal sealed record DaemonState(
    [property: JsonPropertyName("pid")] int Pid,
    [property: JsonPropertyName("started")] double Started);
