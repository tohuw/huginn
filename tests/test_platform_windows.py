"""Windows adapter behavior that can be verified without a Windows host."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from huginn.platform.windows import WindowsPlatform


def test_windows_process_relationships_use_cim_rows():
    adapter = WindowsPlatform()
    with patch("huginn.platform.windows._process_json", return_value=[{"ProcessId": 4}, {"ProcessId": 9}]):
        assert adapter.children(2) == [4, 9]

    with patch("huginn.platform.windows._process_json", return_value=[{"ParentProcessId": 7}]):
        assert adapter.parent(9) == 7


def test_windows_cwd_extracts_codex_flag():
    adapter = WindowsPlatform()
    row = {"CommandLine": 'codex.exe --cwd "C:\\Users\\me\\project"'}
    with patch("huginn.platform.windows._process_json", return_value=[row]):
        assert adapter.process_cwd(10) == "C:\\Users\\me\\project"


def test_windows_terminal_reports_exact_tab_degradation():
    adapter = WindowsPlatform()
    with patch.object(adapter, "parent", return_value=None), \
         patch.object(adapter, "find_processes", return_value=[20]), \
         patch.object(adapter, "_window_for_processes", return_value=100), \
         patch.object(adapter, "_raise_window", return_value=True):
        result = adapter.focus_terminal(10)
    assert result.ok
    assert result.target == "Windows Terminal"
    assert result.detail == "Windows Terminal focused; exact tab unavailable"


def test_windows_vscode_uses_existing_window():
    adapter = WindowsPlatform()
    process = MagicMock()
    with patch("huginn.platform.windows.shutil.which", return_value="code.cmd"), \
         patch("huginn.platform.windows.subprocess.Popen", return_value=process) as popen:
        result = adapter.focus_vscode("C:\\work")
    assert result.ok
    assert popen.call_args.args[0] == ["code.cmd", "--reuse-window", "C:\\work"]
