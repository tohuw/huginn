"""Windows adapter behavior that can be verified without a Windows host."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from huginn.platform.windows import WindowsPlatform


def test_windows_process_relationships_use_cim_rows():
    adapter = WindowsPlatform()
    with patch("huginn.platform.windows._process_json", return_value=[{"ProcessId": 4}, {"ProcessId": 9}]):
        assert adapter.children(2) == [4, 9]

    # None means "no process snapshot here", which is the path off Windows and
    # the fallback when a snapshot cannot be taken.
    with patch("huginn.platform.windows._toolhelp_parents", return_value=None), \
         patch("huginn.platform.windows._process_json", return_value=[{"ParentProcessId": 7}]):
        assert adapter.parent(9) == 7


def test_parent_prefers_one_process_snapshot_over_a_powershell_call_each():
    """Ancestry is walked per jump, so a subprocess per level is not free."""
    adapter = WindowsPlatform()
    with patch("huginn.platform.windows._toolhelp_parents", return_value={9: 7, 7: 0}) as snapshot, \
         patch("huginn.platform.windows._process_json") as shelled_out:
        assert adapter.parent(9) == 7
        assert adapter.parent(7) is None  # pid 0 is not a parent
        assert adapter.parent(404) is None
    assert snapshot.called
    shelled_out.assert_not_called()


def test_an_exited_process_is_not_alive_just_because_a_handle_opens():
    """The roster filled with corpses because opening a handle was the test.

    A Windows process object outlives the process while any handle to it is
    held -- and Huginn holds them -- so OpenProcess kept succeeding for exited
    sessions. They stayed in the roster in whatever state they died in, and
    triage reported them as live sessions competing for a worktree. The
    zero-timeout wait is what distinguishes the two: WAIT_TIMEOUT means still
    running, WAIT_OBJECT_0 means exited.
    """
    adapter = WindowsPlatform()
    kernel32 = MagicMock()
    kernel32.OpenProcess.return_value = 1234

    # create=True: ctypes has no WinDLL off Windows, and patching an attribute
    # that does not exist is an error rather than a skip. This test needs no
    # Windows host -- kernel32 is a mock either way -- so creating the name is
    # what keeps it running on the macOS and Linux CI jobs.
    with patch("huginn.platform.windows.os.name", "nt"), \
         patch("huginn.platform.windows.ctypes.WinDLL", create=True,
               return_value=kernel32):
        kernel32.WaitForSingleObject.return_value = 0x102  # WAIT_TIMEOUT
        assert adapter.pid_alive(10) is True

        kernel32.WaitForSingleObject.return_value = 0  # WAIT_OBJECT_0: exited
        assert adapter.pid_alive(10) is False

    assert kernel32.CloseHandle.call_count == 2  # never leaks the handle


def test_windows_cwd_extracts_codex_flag():
    adapter = WindowsPlatform()
    row = {"CommandLine": 'codex.exe --cwd "C:\\Users\\me\\project"'}
    with patch("huginn.platform.windows._process_json", return_value=[row]):
        assert adapter.process_cwd(10) == "C:\\Users\\me\\project"


def test_windows_terminal_reports_exact_tab_degradation():
    """Focus half-succeeds here, and the detail has to say why.

    Windows Terminal runs every window and every tab in one process behind one
    top-level HWND, so ancestry resolves every session on the machine to the
    same window -- and its UI Automation tree distinguishes them no better:
    every element reports the same ProcessId, and tab items carry no
    AutomationId, only whatever title the shell set. Raising the window can
    therefore leave a different session's tab on screen, which is what made
    jump look broken rather than limited.
    """
    adapter = WindowsPlatform()
    with patch.object(adapter, "parent", return_value=None), \
         patch.object(adapter, "find_processes", return_value=[20]), \
         patch.object(adapter, "_window_for_processes", return_value=100), \
         patch.object(adapter, "_raise_window", return_value=True):
        result = adapter.focus_terminal(10)
    assert result.ok
    assert result.target == "Windows Terminal"
    # The reason, not just the symptom: "exact tab unavailable" reads as a
    # defect in Huginn, and a user cannot act on it.
    assert "one window" in result.detail
    assert "tab could not be selected" in result.detail


def test_focus_prefers_the_window_hosting_this_session():
    """With several terminals open, the session's own window must win.

    A Windows Terminal shell reaches WindowsTerminal.exe by walking parents, so
    the ancestry identifies the right window. Searching every terminal pid at
    once returns whichever window is topmost instead, which focused a different
    terminal than the one the session is in.
    """
    adapter = WindowsPlatform()
    parents = {10: 11, 11: 12, 12: None}
    searched: list[list[int]] = []

    def window_for(pids):
        searched.append(list(pids))
        return 100

    with patch.object(adapter, "parent", side_effect=parents.get), \
         patch.object(adapter, "find_processes", return_value=[77, 88]), \
         patch.object(adapter, "_window_for_processes", side_effect=window_for), \
         patch.object(adapter, "_raise_window", return_value=True) as raised:
        result = adapter.focus_terminal(10)

    assert result.ok
    assert raised.call_args.args == (100,)
    # Searched once, on the ancestry alone -- not merged with every terminal --
    # and nearest ancestor first, so the terminal outranks whatever launched it.
    assert searched == [[10, 11, 12]]


def test_a_tray_app_with_a_hidden_window_can_still_be_activated():
    """Jump reported "window not found" for apps that were plainly running.

    Claude Desktop and ChatGPT keep running with their main window hidden once
    you close it -- the ordinary state for a tray app, and the state they are in
    most of the time. Observed live: both had a real captioned top-level window
    owned by the right process, and IsWindowVisible said False for both, so the
    visibility test rejected the one window worth having.
    """
    adapter = WindowsPlatform()
    shown = []
    user32 = MagicMock()
    user32.ShowWindow.side_effect = lambda hwnd, cmd: shown.append((hwnd, cmd))

    with patch.object(adapter, "find_processes", return_value=[7]), \
         patch.object(adapter, "_raise_window", return_value=True), \
         patch.object(WindowsPlatform, "_window_for_processes",
                      side_effect=[None, 4242]) as lookup, \
         patch("huginn.platform.windows.os.name", "nt"), \
         patch("huginn.platform.windows.ctypes.windll", create=True) as windll:
        windll.user32 = user32
        result = adapter.activate_app("Claude")

    # Visible first, hidden only as a fallback: a shown window still wins.
    assert lookup.call_args_list[0].kwargs.get("require_visible") in (None, True)
    assert lookup.call_args_list[1].kwargs["require_visible"] is False
    # SW_SHOW, because raising a window without WS_VISIBLE shows nobody anything.
    assert shown == [(4242, 5)]
    assert result.ok
    assert "hidden" in result.detail


def test_a_visible_app_window_is_not_re_shown():
    """SW_SHOW on an already-visible window is noise, and can un-maximize."""
    adapter = WindowsPlatform()
    user32 = MagicMock()

    with patch.object(adapter, "find_processes", return_value=[7]), \
         patch.object(adapter, "_raise_window", return_value=True), \
         patch.object(WindowsPlatform, "_window_for_processes", return_value=99), \
         patch("huginn.platform.windows.os.name", "nt"), \
         patch("huginn.platform.windows.ctypes.windll", create=True) as windll:
        windll.user32 = user32
        result = adapter.activate_app("Claude")

    user32.ShowWindow.assert_not_called()
    assert result.ok
    assert result.detail is None


def test_an_app_that_is_not_running_still_reports_not_found():
    adapter = WindowsPlatform()
    with patch.object(adapter, "find_processes", return_value=[]), \
         patch.object(WindowsPlatform, "_window_for_processes", return_value=None):
        result = adapter.activate_app("Claude")
    assert not result.ok
    assert "not found" in result.detail


def test_hidden_windows_stay_out_of_terminal_focus():
    """The relaxation must not leak into focus_terminal.

    Explorer's invisible bookkeeping windows are exactly what the visibility
    test exists to reject, and a terminal that is not on screen is not the
    terminal hosting a session.
    """
    user32 = MagicMock()
    user32.IsWindowVisible.return_value = False
    user32.GetAncestor.side_effect = lambda hwnd, _flag: hwnd
    user32.GetWindowLongW.return_value = 0
    user32.GetWindowTextLengthW.return_value = 12

    with patch("huginn.platform.windows.os.name", "nt"), \
         patch("huginn.platform.windows.ctypes.windll", create=True) as windll:
        windll.user32 = user32
        assert WindowsPlatform._is_app_window(1) is False
        assert WindowsPlatform._is_app_window(1, require_visible=False) is True


def test_helper_windows_are_not_focus_targets():
    """Explorer's visible bookkeeping windows must never be picked.

    ThumbnailDeviceHelperWnd, DummyDWMListenerWindow, Progman and Shell_TrayWnd
    are all visible and all set WS_EX_TOOLWINDOW. Explorer is in a terminal's
    ancestry, so before this filter jump raised an invisible helper and looked
    like it did nothing.
    """
    user32 = MagicMock()
    user32.IsWindowVisible.return_value = True
    user32.GetAncestor.side_effect = lambda hwnd, _flag: hwnd
    # 1: a tool window. 2: a real window. 3: real but captionless.
    user32.GetWindowLongW.side_effect = lambda hwnd, _idx: 0x80 if hwnd == 1 else 0
    user32.GetWindowTextLengthW.side_effect = lambda hwnd: 0 if hwnd == 3 else 12

    with patch("huginn.platform.windows.os.name", "nt"), \
         patch("huginn.platform.windows.ctypes.windll", create=True) as windll:
        windll.user32 = user32
        assert not WindowsPlatform._is_app_window(1)
        assert WindowsPlatform._is_app_window(2)
        assert not WindowsPlatform._is_app_window(3)

        user32.IsWindowVisible.return_value = False
        assert not WindowsPlatform._is_app_window(2)


def test_focus_falls_back_to_any_terminal_when_ancestry_is_broken():
    adapter = WindowsPlatform()
    with patch.object(adapter, "parent", return_value=None), \
         patch.object(adapter, "find_processes", return_value=[77]), \
         patch.object(adapter, "_window_for_processes", side_effect=[None, 555]), \
         patch.object(adapter, "_raise_window", return_value=True) as raised:
        result = adapter.focus_terminal(10)
    assert result.ok
    assert raised.call_args.args == (555,)


def test_windows_terminal_reports_a_refused_foreground_change():
    adapter = WindowsPlatform()
    with patch.object(adapter, "parent", return_value=None), \
         patch.object(adapter, "find_processes", return_value=[20]), \
         patch.object(adapter, "_window_for_processes", return_value=100), \
         patch.object(adapter, "_raise_window", return_value=False):
        result = adapter.focus_terminal(10)
    assert not result.ok
    assert "foreground" in result.detail


def test_windows_vscode_uses_existing_window():
    adapter = WindowsPlatform()
    process = MagicMock()
    with patch("huginn.platform.windows.shutil.which", return_value="code.cmd"), \
         patch("huginn.platform.windows.subprocess.Popen", return_value=process) as popen:
        result = adapter.focus_vscode("C:\\work")
    assert result.ok
    assert popen.call_args.args[0] == ["code.cmd", "--reuse-window", "C:\\work"]
