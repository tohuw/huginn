"""No LLM subprocess should outlive its caller -- timeout, cancellation, or
a non-zero exit -- issue #16. Drives real child processes (small shell
scripts standing in for the claude/codex binaries) rather than mocking
asyncio.subprocess, so this actually proves the kill+reap happens at the OS
level, not just that the right method got called."""
from __future__ import annotations

import asyncio
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from huginn.llm.providers import ClaudeCLI, CodexCLI


def _script(tmp: Path, name: str, body: str) -> str:
    path = tmp / name
    path.write_text(f"#!/bin/sh\n{body}\n")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


class ClaudeRunTextLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    async def test_kills_and_reaps_on_timeout(self):
        # sleep 0.1 then the real hang, so the pid write always lands well
        # before the 0.5s run_text timeout regardless of shell-spawn jitter.
        pidfile = self.tmp_path / "pid"
        script = _script(self.tmp_path, "claude",
                         f'echo $$ > {pidfile}\nsleep 0.1\nexec sleep 5\n')
        with patch("huginn.llm.providers.claude_binary", return_value=script):
            with self.assertRaisesRegex(RuntimeError, "timed out"):
                await ClaudeCLI().run_text("hi", timeout=0.5)
        pid = int(pidfile.read_text().strip())
        await asyncio.sleep(0.1)   # let the kill signal land
        self.assertFalse(_alive(pid), "child survived a timed-out run_text()")

    async def test_raises_on_nonzero_exit_with_bounded_stderr(self):
        script = _script(self.tmp_path, "claude",
                         f'echo {"x" * 5000} >&2\nexit 3\n')
        with patch("huginn.llm.providers.claude_binary", return_value=script):
            with self.assertRaises(RuntimeError) as ctx:
                await ClaudeCLI().run_text("hi", timeout=5)
        self.assertLessEqual(len(str(ctx.exception)), 320)

    async def test_normal_completion_leaves_no_zombie(self):
        pidfile = self.tmp_path / "pid"
        script = _script(self.tmp_path, "claude", f'echo $$ > {pidfile}\necho done\n')
        with patch("huginn.llm.providers.claude_binary", return_value=script):
            out = await ClaudeCLI().run_text("hi", timeout=5)
        self.assertEqual(out, "done")
        pid = int(pidfile.read_text().strip())
        self.assertFalse(_alive(pid))


class ClaudeStreamLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    async def test_cancellation_kills_the_child(self):
        pidfile = self.tmp_path / "pid"
        script = _script(self.tmp_path, "claude", f'echo $$ > {pidfile}\nexec sleep 5\n')

        async def consume():
            with patch("huginn.llm.providers.claude_binary", return_value=script):
                async for _chunk in ClaudeCLI().stream("hi"):
                    pass  # pragma: no cover -- script never emits stdout

        task = asyncio.create_task(consume())
        for _ in range(50):
            if pidfile.exists():
                break
            await asyncio.sleep(0.05)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        pid = int(pidfile.read_text().strip())
        await asyncio.sleep(0.1)
        self.assertFalse(_alive(pid), "child survived stream() cancellation")

    async def test_raises_on_nonzero_exit_with_bounded_stderr(self):
        script = _script(self.tmp_path, "claude",
                         f'echo {"y" * 5000} >&2\nexit 1\n')
        with patch("huginn.llm.providers.claude_binary", return_value=script):
            with self.assertRaises(RuntimeError) as ctx:
                async for _ in ClaudeCLI().stream("hi"):
                    pass
        self.assertLessEqual(len(str(ctx.exception)), 320)

    async def test_yields_text_deltas_and_leaves_no_zombie(self):
        pidfile = self.tmp_path / "pid"
        event = '{"type":"stream_event","event":{"delta":{"type":"text_delta","text":"hi"}}}'
        script = _script(self.tmp_path, "claude", f"echo $$ > {pidfile}\necho '{event}'\n")
        with patch("huginn.llm.providers.claude_binary", return_value=script):
            chunks = [c async for c in ClaudeCLI().stream("hi")]
        self.assertEqual(chunks, ["hi"])
        pid = int(pidfile.read_text().strip())
        self.assertFalse(_alive(pid))


class CodexStreamLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    async def test_cancellation_kills_the_child(self):
        pidfile = self.tmp_path / "pid"
        # CodexCLI passes the prompt as an argv element, not stdin.
        script = _script(self.tmp_path, "codex", f'echo $$ > {pidfile}\nexec sleep 5\n')

        async def consume():
            with patch("huginn.llm.providers.CODEX_BIN", script):
                async for _chunk in CodexCLI().stream("hi"):
                    pass  # pragma: no cover

        task = asyncio.create_task(consume())
        for _ in range(50):
            if pidfile.exists():
                break
            await asyncio.sleep(0.05)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        pid = int(pidfile.read_text().strip())
        await asyncio.sleep(0.1)
        self.assertFalse(_alive(pid), "child survived stream() cancellation")

    async def test_separates_distinct_agent_message_updates(self):
        first = '{"type":"item.updated","item":{"type":"agent_message","text":"I will inspect."}}'
        final = '{"type":"item.completed","item":{"type":"agent_message","text":"The result is ready."}}'
        script = _script(self.tmp_path, "codex", f"echo '{first}'\necho '{final}'")
        with patch("huginn.llm.providers.CODEX_BIN", script):
            chunks = [c async for c in CodexCLI().stream("hi")]
        self.assertEqual(chunks, ["I will inspect.", "\n", "The result is ready."])


if __name__ == "__main__":
    unittest.main()
