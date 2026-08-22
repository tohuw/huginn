"""Native Windows process inspection and best-effort focus routing."""
from __future__ import annotations

import ctypes
import datetime as dt
import json
import logging
import os
import re
import shutil
import subprocess
import urllib.parse
from ctypes import wintypes
from pathlib import Path

from .base import FocusResult, Platform

log = logging.getLogger(__name__)


CREATE_NO_WINDOW = 0x08000000

GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x00000080
GA_ROOT = 2

SYNCHRONIZE = 0x0010_0000
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
WAIT_TIMEOUT = 0x0000_0102
ERROR_ACCESS_DENIED = 5


#: How a pipe from another program is decoded. ``text=True`` alone uses the
#: *locale* encoding -- cp1252 on Windows -- and this reads back the command line
#: of every process on the machine. One arrow or em dash in any of them raises
#: UnicodeDecodeError out of a routine roster scan, which is the same failure
#: that made Ask hang and that killed the Claude scan. ``errors="replace"``
#: because losing a glyph from a command line beats losing the whole scan.
_PIPE_TEXT = {"encoding": "utf-8", "errors": "replace"}


def _same_dir(reported: str | None, target: str) -> bool:
    r"""Does a pane's reported cwd name the same directory as ``target``?

    WezTerm reports it as a URL -- ``file:///C:/Users/me/repo/`` -- so this
    unwraps the scheme, undoes percent-encoding, and normalises separators,
    case and the trailing slash before comparing. Comparing the raw strings
    matches nothing on Windows, where Huginn holds ``C:\Users\me\repo``.
    """
    if not reported:
        return False
    path = str(reported)
    if path.startswith("file://"):
        path = urllib.parse.unquote(urllib.parse.urlparse(path).path)
        # A Windows URL path is "/C:/..."; the leading slash is not part of it.
        if len(path) > 2 and path[0] == "/" and path[2] == ":":
            path = path[1:]
    try:
        return os.path.normcase(os.path.abspath(path)) == target
    except (OSError, ValueError):
        return False


def _powershell(script: str, timeout: float = 5) -> str:
    executable = shutil.which("pwsh") or shutil.which("powershell") or "powershell.exe"
    try:
        return subprocess.run(
            [executable, "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, timeout=timeout, **_PIPE_TEXT,
            creationflags=CREATE_NO_WINDOW if os.name == "nt" else 0,
        ).stdout.strip()
    except (subprocess.SubprocessError, OSError):
        return ""


class _PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", ctypes.c_wchar * 260),
    ]


def _toolhelp_parents() -> dict[int, int] | None:
    """Every pid's parent, from one process snapshot. None if unavailable.

    Focus walks up to twelve levels of ancestry, and each level used to be a
    separate ``Get-CimInstance`` -- one PowerShell process per hop, measured at
    0.42s each, so a jump spent about two seconds finding a window before it
    could raise it. The whole table comes back from a single snapshot here, so
    the walk costs one call regardless of depth. ``_process_json`` remains the
    fallback and the only path off Windows.
    """
    if os.name != "nt":
        return None
    kernel32 = ctypes.windll.kernel32
    snapshot = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)  # TH32CS_SNAPPROCESS
    if snapshot == -1 or not snapshot:
        return None
    try:
        entry = _PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(_PROCESSENTRY32W)
        if not kernel32.Process32FirstW(snapshot, ctypes.byref(entry)):
            return None
        parents: dict[int, int] = {}
        while True:
            parents[entry.th32ProcessID] = entry.th32ParentProcessID
            if not kernel32.Process32NextW(snapshot, ctypes.byref(entry)):
                break
        return parents
    finally:
        kernel32.CloseHandle(snapshot)


#: What a Windows executable's file name may contain before it is allowed into
#: a WMI filter. The filter is interpolated into a PowerShell double-quoted
#: string, where ``$(...)`` is a subexpression PowerShell *evaluates* and a
#: quote ends the string -- so escaping the single quotes the WMI syntax needs
#: is not, on its own, enough to make an arbitrary name safe to put there.
#: Every caller today passes a literal, and this is what keeps that from being
#: the only thing standing between a process name and a shell.
_SAFE_PROCESS_NAME = re.compile(r"\A[A-Za-z0-9 ._+-]{1,120}\Z")


#: The only file names this platform will execute from a recorded terminal
#: identity. The path beside them is not checked and cannot usefully be: WezTerm
#: is installed wherever its owner put it. The *name* is the part that has to
#: hold, because the value arrives in a hook payload -- written by the session's
#: own environment, which a repository's env file can set -- and is then run,
#: much later, when someone clicks jump. A recorded path is a place to look for
#: wezterm, never a licence to run something else.
_WEZTERM_BINARIES = {"wezterm", "wezterm.exe"}


def _wezterm_binary(recorded: object) -> str | None:
    """``recorded`` if it names the WezTerm CLI, "wezterm" if nothing was, else None."""
    if not recorded:
        return "wezterm"
    if not isinstance(recorded, str):
        return None
    if os.path.basename(recorded).lower() not in _WEZTERM_BINARIES:
        return None
    return recorded


def _process_json(where: str) -> list[dict]:
    raw = _powershell(
        f"Get-CimInstance Win32_Process -Filter \"{where}\" | "
        "Select-Object ProcessId,ParentProcessId,Name,CreationDate,CommandLine,ExecutablePath"
        " | ConvertTo-Json -Compress"
    )
    if not raw:
        return []
    try:
        value = json.loads(raw)
        return value if isinstance(value, list) else [value]
    except json.JSONDecodeError:
        return []


class WindowsPlatform(Platform):
    def pid_alive(self, pid: int) -> bool:
        """Is this process running -- not merely still addressable?

        Opening a handle is not the question, and answering it that way is why
        the roster filled up with corpses. A Windows process object outlives the
        process itself for as long as anyone holds a handle to it, and Huginn is
        one of those holders, so ``OpenProcess`` kept succeeding for sessions
        that had exited minutes earlier. Every dead session stayed in the roster
        in whatever state it died in, and ``triage`` reported them as live
        sessions competing for a worktree.

        A zero-timeout wait is the actual question: a process object is
        unsignalled while it runs and signalled the instant it exits, so an
        exited-but-still-open handle answers correctly.
        """
        if os.name != "nt":
            return bool(_process_json(f"ProcessId={int(pid)}"))
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.OpenProcess(SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION,
                                      False, int(pid))
        if not handle:
            # Owned by another user: it exists, which is what was asked.
            return ctypes.get_last_error() == ERROR_ACCESS_DENIED
        try:
            return kernel32.WaitForSingleObject(handle, 0) == WAIT_TIMEOUT
        finally:
            kernel32.CloseHandle(handle)

    def process_start_time(self, pid: int) -> float | None:
        if os.name == "nt":
            handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))
            if not handle:
                return None
            created = wintypes.FILETIME()
            exited = wintypes.FILETIME()
            kernel = wintypes.FILETIME()
            user = wintypes.FILETIME()
            try:
                if ctypes.windll.kernel32.GetProcessTimes(
                    handle, ctypes.byref(created), ctypes.byref(exited),
                    ctypes.byref(kernel), ctypes.byref(user),
                ):
                    ticks = (created.dwHighDateTime << 32) | created.dwLowDateTime
                    return ticks / 10_000_000 - 11_644_473_600
            finally:
                ctypes.windll.kernel32.CloseHandle(handle)
            return None
        rows = _process_json(f"ProcessId={int(pid)}")
        if not rows or not rows[0].get("CreationDate"):
            return None
        try:
            return dt.datetime.fromisoformat(rows[0]["CreationDate"]).timestamp()
        except (TypeError, ValueError):
            return None

    def children(self, pid: int) -> list[int]:
        return [int(row["ProcessId"]) for row in _process_json(f"ParentProcessId={int(pid)}")]

    def parent(self, pid: int) -> int | None:
        parents = _toolhelp_parents()
        if parents is not None:
            return parents.get(int(pid)) or None
        rows = _process_json(f"ProcessId={int(pid)}")
        value = int(rows[0].get("ParentProcessId") or 0) if rows else 0
        return value or None

    def process_cwd(self, pid: int) -> str | None:
        # Win32_Process does not expose the PEB current directory. Command-line
        # cwd flags are useful for Codex/VS Code, but callers must tolerate None.
        rows = _process_json(f"ProcessId={int(pid)}")
        command = str(rows[0].get("CommandLine") or "") if rows else ""
        for flag in ("--cwd", "-C"):
            marker = f"{flag} "
            if marker in command:
                value = command.split(marker, 1)[1].strip()
                if value.startswith('"'):
                    return value[1:].split('"', 1)[0]
                # ``split()[0]`` on its own raises IndexError when the flag is
                # the last thing on the line -- `codex --cwd ` with nothing
                # after it leaves the empty string here, and an empty split is
                # an empty list. A command line is external input, and this is
                # reached from a roster scan, so the crash would arrive as a
                # failed refresh rather than as anything naming a command line.
                parts = value.split()
                return parts[0] if parts else None
        return None

    def process_name(self, pid: int) -> str | None:
        # Guarded like every sibling here, and for a reason this one hits
        # hardest: callers reach it by walking children(), so the pid is one
        # observed a moment ago rather than one they hold a handle to. A short
        # lived shell that exits in between leaves no row, and the unguarded
        # rows[0] raised IndexError out of a routine scan -- taking down
        # `huginn doctor`, and any roster refresh that crossed the same exit.
        rows = _process_json(f"ProcessId={int(pid)}")
        return (str(rows[0].get("Name") or "") or None) if rows else None

    def process_path(self, pid: int) -> str | None:
        rows = _process_json(f"ProcessId={int(pid)}")
        return (str(rows[0].get("ExecutablePath") or "") or None) if rows else None

    def process_tty(self, pid: int) -> str | None:
        return None

    def find_processes(self, executable: str) -> list[int]:
        name = executable if executable.lower().endswith(".exe") else f"{executable}.exe"
        if not _SAFE_PROCESS_NAME.match(name):
            # Not an error worth raising: no process is named this, so the
            # honest answer to "which pids run it" is none.
            log.debug("Refusing to search for an implausible process name")
            return []
        escaped = name.replace("'", "''")
        return [int(row["ProcessId"]) for row in _process_json(f"Name='{escaped}'")]

    @staticmethod
    def _is_app_window(hwnd: int, *, require_visible: bool = True) -> bool:
        """Is this a window a user could actually switch to?

        ``IsWindowVisible`` alone is far too generous. Explorer keeps a pile of
        visible bookkeeping windows -- ``ThumbnailDeviceHelperWnd``,
        ``DummyDWMListenerWindow``, ``Progman``, ``Shell_TrayWnd`` -- and taking
        the first visible one meant focus targeted an invisible helper and
        appeared to do nothing at all. Every one of those sets
        ``WS_EX_TOOLWINDOW``, which is precisely the "keep me out of the
        switcher" flag, so honouring it is what separates them from a terminal.
        A real top-level window with a caption is what remains.

        ``require_visible=False`` is for *activating an app*, where a hidden
        window is the thing you want rather than the thing to skip. Claude
        Desktop and ChatGPT both keep running with their main window hidden when
        you close it -- the ordinary state for a tray app -- and asking Huginn to
        jump to one is asking for it to be shown again. The other three checks
        still stand, so an invisible helper is still rejected; only the test that
        was answering the wrong question is dropped.
        """
        user32 = ctypes.windll.user32
        if require_visible and not user32.IsWindowVisible(hwnd):
            return False
        if user32.GetAncestor(hwnd, GA_ROOT) != hwnd:
            return False
        if user32.GetWindowLongW(hwnd, GWL_EXSTYLE) & WS_EX_TOOLWINDOW:
            return False
        return user32.GetWindowTextLengthW(hwnd) > 0

    @staticmethod
    def _window_for_processes(pids, *, require_visible: bool = True) -> int | None:
        """First app window owned by these pids, in the order given.

        Order is the whole point when the caller passes a process ancestry:
        launching a terminal from Explorer puts Explorer in that ancestry, and
        Explorer owns perfectly legitimate windows of its own. Nearest ancestor
        first means the terminal hosting the session wins over the file manager
        that happened to start it.

        ``require_visible`` is passed through to :meth:`_is_app_window`; see
        there for why activating an app wants a hidden window and focusing a
        terminal does not.
        """
        if os.name != "nt":
            return None
        ordered = list(pids)
        wanted = set(ordered)
        user32 = ctypes.windll.user32
        by_pid: dict[int, int] = {}
        callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        @callback_type
        def callback(hwnd, _lparam):
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            # EnumWindows runs in Z-order, so the first hit per pid is that
            # process's frontmost window.
            if (pid.value in wanted and pid.value not in by_pid
                    and WindowsPlatform._is_app_window(
                        hwnd, require_visible=require_visible)):
                by_pid[pid.value] = hwnd
            return True

        user32.EnumWindows(callback, 0)
        return next((by_pid[pid] for pid in ordered if pid in by_pid), None)

    @staticmethod
    def _raise_window(hwnd: int) -> bool:
        """Bring a window to the foreground, defeating the foreground lock.

        Windows only lets the current foreground process hand focus away, so a
        background daemon's bare ``SetForegroundWindow`` is refused. Attaching
        to the foreground thread's input queue for the duration is the
        documented way around it, and without it a jump from the dashboard
        fails on a machine where nothing is wrong.
        """
        if os.name != "nt":
            return False
        user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
        foreground = user32.GetForegroundWindow()
        if foreground == hwnd:
            return True
        ours = kernel32.GetCurrentThreadId()
        threads = {
            user32.GetWindowThreadProcessId(hwnd, None),
            user32.GetWindowThreadProcessId(foreground, None) if foreground else 0,
        } - {0, ours}
        attached = [tid for tid in threads if user32.AttachThreadInput(ours, tid, True)]
        try:
            # Only un-minimize. SW_RESTORE on a maximized window un-maximizes
            # it, so restoring unconditionally would resize a terminal the user
            # deliberately maximized every time they jumped to it.
            if user32.IsIconic(hwnd):
                user32.ShowWindow(hwnd, 9)  # SW_RESTORE
            user32.BringWindowToTop(hwnd)
            return bool(user32.SetForegroundWindow(hwnd))
        finally:
            for tid in attached:
                user32.AttachThreadInput(ours, tid, False)

    def _wezterm_control(self) -> tuple[str, str] | None:
        """A running WezTerm's cli binary and control socket, found from outside.

        The hook cannot help here: it only ever runs for a session that has done
        something since Huginn last started, and an idle tab may not have. So
        this reconstructs what a pane would have reported, from the GUI process
        itself -- its image path gives the sibling ``wezterm`` that speaks the
        control protocol, and its pid names the socket, which WezTerm writes as
        ``gui-sock-<pid>``.
        """
        for pid in self.find_processes("wezterm-gui"):
            image = self.process_path(pid)
            if not image:
                continue
            cli = os.path.join(os.path.dirname(image),
                               "wezterm.exe" if os.name == "nt" else "wezterm")
            socket = Path.home() / ".local" / "share" / "wezterm" / f"gui-sock-{pid}"
            if os.path.exists(cli) and socket.exists():
                return cli, str(socket)
        return None

    def discover_pane(self, cwd: str) -> dict[str, str] | None:
        """The pane whose working directory is ``cwd``, if exactly one is.

        A fallback for the sessions the hook has not reached yet, which on a
        machine that has been running a while is most of them: a tab sitting
        idle fires no hooks, so it carries no recorded pane and jump falls back
        to raising a window -- the behaviour this whole line of work exists to
        replace. WezTerm reports each pane's cwd, and Huginn knows each
        session's, so the mapping is usually already there for the asking.

        **Exactly one, or nothing.** Two sessions in one repository is ordinary,
        and a guess between them would send the user to the wrong tab
        confidently. Raising the window is a worse answer that is at least
        honestly approximate; picking the wrong tab is not.
        """
        if not cwd:
            return None
        control = self._wezterm_control()
        if control is None:
            return None
        cli, socket = control
        env = dict(os.environ)
        env["WEZTERM_UNIX_SOCKET"] = socket
        try:
            done = subprocess.run([cli, "cli", "list", "--format", "json"],
                                  capture_output=True, timeout=10, env=env,
                                  **_PIPE_TEXT,
                                  creationflags=CREATE_NO_WINDOW if os.name == "nt" else 0)
            panes = json.loads(done.stdout or "[]")
        except (OSError, subprocess.SubprocessError, ValueError):
            return None
        target = os.path.normcase(os.path.abspath(cwd))
        matches = {p.get("pane_id") for p in panes
                   if isinstance(p, dict) and _same_dir(p.get("cwd"), target)}
        if len(matches) != 1:
            return None
        return {"kind": "wezterm", "pane": str(matches.pop()),
                "socket": socket, "executable": cli}

    def focus_pane(self, terminal: dict[str, str]) -> FocusResult:
        """Focus an exact tab, using coordinates its terminal issued.

        This is the answer to what ``focus_terminal`` cannot do. A pid names a
        process, and Windows Terminal hosts every tab of every window in one
        process behind one HWND -- so no amount of window hunting can pick a
        tab. WezTerm issues each pane an id and takes it back over a control
        socket, which turns focus from a search into an address.

        Everything needed came from the pane's own environment via the hook, so
        nothing is discovered here: the executable that speaks the protocol and
        the socket of the GUI hosting that pane are both recorded. The socket
        especially -- the daemon runs outside any pane and could not find it.
        """
        if terminal.get("kind") != "wezterm":
            return FocusResult(False, None, f"unknown terminal {terminal.get('kind')!r}")
        pane = terminal.get("pane")
        if not pane:
            return FocusResult(False, None, "no pane recorded for this session")
        executable = _wezterm_binary(terminal.get("executable"))
        if executable is None:
            return FocusResult(False, None, "recorded terminal binary is not wezterm")
        env = dict(os.environ)
        socket = terminal.get("socket")
        if socket:
            env["WEZTERM_UNIX_SOCKET"] = socket
        try:
            done = subprocess.run(
                [executable, "cli", "activate-pane", "--pane-id", str(pane)],
                capture_output=True, timeout=10, env=env, **_PIPE_TEXT,
                creationflags=CREATE_NO_WINDOW if os.name == "nt" else 0)
        except (OSError, subprocess.SubprocessError) as exc:
            return FocusResult(False, None, f"WezTerm CLI unavailable ({exc.__class__.__name__})")
        if done.returncode != 0:
            # The commonest reason is that the terminal has since closed, which
            # is a fact about the world rather than a failure to report darkly.
            detail = (done.stderr or "").strip().splitlines()
            return FocusResult(False, None,
                               f"WezTerm refused: {detail[-1] if detail else 'unknown error'}")
        return FocusResult(True, "WezTerm", f"WezTerm pane {pane} focused")

    def focus_terminal(self, pid: int | None, tty: str | None = None) -> FocusResult:
        # The session's own ancestry, nearest first, and searched alone. A shell
        # hosted by Windows Terminal reaches WindowsTerminal.exe by walking
        # parents (pwsh -> claude -> pwsh -> WindowsTerminal), so the ancestry
        # names the window actually hosting this session. It keeps walking past
        # the terminal to whatever launched it -- usually Explorer -- which is
        # why order matters as much as membership: Explorer owns real windows
        # too, and an unordered set let a File Explorer window outrank the
        # terminal.
        ancestry: list[int] = []
        current = pid
        for _ in range(12):
            if not current or current in ancestry:
                break
            ancestry.append(current)
            current = self.parent(current)
        hwnd = self._window_for_processes(ancestry)
        if not hwnd:
            # Ancestry can be broken -- a reparented shell, or a session Huginn
            # only knows from a transcript. One terminal window is still a
            # better answer than none, and with a single window it is the right
            # one; the detail says the tab is not exact either way.
            hwnd = self._window_for_processes(set(self.find_processes("WindowsTerminal")))
        if not hwnd:
            return FocusResult(False, None, "Windows Terminal window not found")
        if not self._raise_window(hwnd):
            return FocusResult(False, None, "Windows Terminal could not be brought to the foreground")
        # Says *why*, because "exact tab unavailable" reads as a bug in Huginn
        # and is not one. Windows Terminal runs every window and every tab in a
        # single process with a single top-level HWND, so ancestry resolves all
        # of a machine's sessions to the same window -- verified by walking four
        # live sessions to one hwnd. Nothing in its UI Automation tree
        # distinguishes them either: every element reports the same ProcessId,
        # and the tab items carry no AutomationId, only the title the shell set.
        # There is no supported mapping from a child process to its tab.
        return FocusResult(
            True, "Windows Terminal",
            "Windows Terminal raised; it runs every tab in one window, so the "
            "session's own tab could not be selected")

    def send_terminal_text(self, pid: int | None, tty: str | None, text: str) -> FocusResult:
        return FocusResult(
            False,
            detail="safe exact-tab steering is not available on Windows Terminal",
        )

    def interrupt_terminal(self, pid: int | None, tty: str | None) -> FocusResult:
        return FocusResult(
            False,
            detail="safe exact-tab interruption is not available on Windows Terminal",
        )

    def focus_vscode(self, cwd: str) -> FocusResult:
        executable = shutil.which("code") or shutil.which("code.cmd")
        if not executable:
            return FocusResult(False, detail="VS Code command not found")
        try:
            subprocess.Popen(
                [executable, "--reuse-window", cwd], stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            return FocusResult(True, "VS Code")
        except OSError:
            return FocusResult(False, detail="VS Code could not be opened")

    def activate_app(self, name: str) -> FocusResult:
        """Bring a desktop app to the front, showing it if it is hidden.

        A visible window is preferred and a hidden one accepted, in that order.
        Both Claude Desktop and ChatGPT keep running with their main window
        hidden after you close it -- the ordinary state for a tray app, and the
        state they are in most of the time. Requiring visibility meant jump
        found no window for either and reported "window not found" about an
        application that was plainly running, which is the bug this fixes.

        A hidden window also has to be *shown*, not merely raised:
        ``SetForegroundWindow`` on a window without WS_VISIBLE does nothing
        anyone can see. ``_raise_window`` deliberately only un-minimizes, so
        that it never un-maximizes a terminal someone maximized on purpose --
        which makes showing the caller's job rather than its own.
        """
        aliases = {
            "ChatGPT": ("ChatGPT", "Codex"),
            "Codex": ("Codex", "ChatGPT"),
            "Claude": ("Claude",),
        }
        pids = {pid for candidate in aliases.get(name, (name,))
                for pid in self.find_processes(candidate)}
        hwnd = self._window_for_processes(pids)
        hidden = False
        if not hwnd:
            hwnd = self._window_for_processes(pids, require_visible=False)
            hidden = bool(hwnd)
        if not hwnd:
            return FocusResult(False, None, f"{name} window not found")
        if hidden and os.name == "nt":
            ctypes.windll.user32.ShowWindow(hwnd, 5)   # SW_SHOW
        ok = bool(self._raise_window(hwnd))
        detail = None
        if ok and hidden:
            detail = f"{name} was hidden; its window was restored"
        return FocusResult(ok, name if ok else None,
                           detail if ok else f"{name} window could not be raised")
