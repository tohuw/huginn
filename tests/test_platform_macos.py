from __future__ import annotations

import os
import tempfile
from pathlib import Path
from unittest.mock import patch

from huginn.platform import macos


def test_iterm_control_delegates_to_configured_native_app() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        helper = Path(tmp) / "Huginn Cisco"
        helper.touch(mode=0o755)
        with patch.dict(os.environ, {"HUGINN_ITERM_CONTROL_APP": str(helper)}), \
                patch("huginn.platform.macos.run", return_value="ok") as run:
            assert macos._iterm_control("--iterm-focus", "/dev/ttys001")

    run.assert_called_once_with([str(helper), "--iterm-focus", "/dev/ttys001"], timeout=10)


def test_focus_script_reveals_hotkey_window_before_retrying_the_search() -> None:
    # A quake-style hotkey window that is dropped up is hidden and excluded
    # from iTerm2's `windows`, so a session living inside it cannot match on
    # the first pass. The script must reveal it and search again rather than
    # give up, and must not rely on `current window` (which is not the hidden
    # hotkey window either) to find it.
    script = macos._OSA_FOCUS_TTY
    first_search = script.index("findAndSelect(targetTty)")
    reveal = script.index("reveal hotkey window")
    second_search = script.index("findAndSelect(targetTty)", reveal)
    assert first_search < reveal < second_search
    assert "current window" not in script
