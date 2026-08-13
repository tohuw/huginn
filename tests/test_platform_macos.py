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
