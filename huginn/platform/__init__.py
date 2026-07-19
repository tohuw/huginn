"""Select the host platform implementation."""
from __future__ import annotations

import sys

from .base import FocusResult, Platform
from .macos import MacOSPlatform
from .windows import WindowsPlatform


def get_platform(name: str | None = None) -> Platform:
    selected = name or sys.platform
    if selected == "win32" or selected.startswith("cygwin"):
        return WindowsPlatform()
    if selected == "darwin":
        return MacOSPlatform()
    # Huginn does not yet ship a Linux UI, but Unix process/focus behavior is
    # closer to the macOS adapter than the Win32 API and remains testable.
    return MacOSPlatform()


platform = get_platform()

__all__ = ["FocusResult", "MacOSPlatform", "Platform", "WindowsPlatform", "get_platform", "platform"]
