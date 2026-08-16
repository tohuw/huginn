"""Operating-system boundary for process inspection and application focus."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(frozen=True)
class FocusResult:
    ok: bool
    target: str | None = None
    detail: str | None = None


class Platform(ABC):
    """Small platform contract used by source scanners and focus routing."""

    @abstractmethod
    def pid_alive(self, pid: int) -> bool: ...

    @abstractmethod
    def process_start_time(self, pid: int) -> float | None: ...

    @abstractmethod
    def children(self, pid: int) -> list[int]: ...

    @abstractmethod
    def parent(self, pid: int) -> int | None: ...

    @abstractmethod
    def process_cwd(self, pid: int) -> str | None: ...

    @abstractmethod
    def process_name(self, pid: int) -> str | None: ...

    @abstractmethod
    def process_path(self, pid: int) -> str | None:
        """Full path to the executable behind ``pid``, or None if unknowable.

        The name alone is not identity. Windows matches process names
        case-insensitively, and the ChatGPT desktop app and the Codex CLI both
        ship a binary called ``codex.exe`` -- so a name match cannot tell a
        desktop app from a command-line tool, and Huginn drew a "ChatGPT
        Desktop is running" tile whenever the CLI ran.
        """

    @abstractmethod
    def process_tty(self, pid: int) -> str | None: ...

    @abstractmethod
    def find_processes(self, executable: str) -> list[int]: ...

    @abstractmethod
    def focus_terminal(self, pid: int | None, tty: str | None = None) -> FocusResult: ...

    @abstractmethod
    def send_terminal_text(self, pid: int | None, tty: str | None, text: str) -> FocusResult: ...

    @abstractmethod
    def interrupt_terminal(self, pid: int | None, tty: str | None) -> FocusResult: ...

    @abstractmethod
    def focus_vscode(self, cwd: str) -> FocusResult: ...

    @abstractmethod
    def activate_app(self, name: str) -> FocusResult: ...
