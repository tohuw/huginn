from unittest.mock import patch

import json
import datetime
import tempfile
from pathlib import Path

from huginn.config import Config
from huginn.daemon import Daemon
from huginn.sources.claude_code import child_shell_count, parse_session_file, pid_matches_start


@patch("huginn.sources.claude_code._platform.children", return_value=[1, 2, 3, 4])
@patch("huginn.sources.claude_code._platform.process_name",
       side_effect=["caffeinate", "zsh", "zsh", "sleep"])
def test_counts_only_direct_shell_children(_names, _children) -> None:
    assert child_shell_count(77968) == 2


@patch("huginn.sources.claude_code._platform.children", return_value=[])
def test_shell_count_degrades_to_zero(_children) -> None:
    assert child_shell_count(77968) == 0


def test_pid_start_compares_claude_utc_time_to_os_epoch() -> None:
    stamp = "Sun Jul 19 12:34:56 2026"
    epoch = datetime.datetime(2026, 7, 19, 12, 34, 56, tzinfo=datetime.timezone.utc).timestamp()
    with patch("huginn.sources.claude_code._platform.process_start_time", return_value=epoch):
        assert pid_matches_start(10, stamp)
    with patch("huginn.sources.claude_code._platform.process_start_time", return_value=epoch + 60):
        assert not pid_matches_start(10, stamp)


def test_owned_internal_pid_is_filtered_independently_of_entrypoint() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "10.json"
        path.write_text(json.dumps({
            "pid": 10, "sessionId": "internal", "kind": "interactive",
            "entrypoint": "future-renamed-entrypoint", "cwd": "/tmp",
        }))
        with patch("huginn.llm.providers.is_internal_pid", return_value=True):
            assert parse_session_file(path) is None


def test_idle_cli_is_not_aged_out_while_its_process_is_alive() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "10.json"
        path.write_text(json.dumps({
            "pid": 10, "sessionId": "open", "kind": "interactive",
            "entrypoint": "cli", "cwd": "/tmp", "status": "idle",
            "statusUpdatedAt": 1,
        }))
        daemon = Daemon(Config({"ui": {"idle_ttl_s": 1}}))
        with patch("huginn.daemon.claude_code.pid_alive", return_value=True), \
             patch("huginn.daemon.claude_code.pid_matches_start", return_value=True):
            daemon._emit_claude_file(path)
        assert daemon.bus.events.get_nowait().kind == "claude.file"
