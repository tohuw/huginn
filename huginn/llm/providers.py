"""LLM providers via headless CLIs — no SDK, no API keys.

Claude: `claude -p` resolved past the user's shell alias; inherits settings env
(local proxy, Max OAuth). ANTHROPIC_API_KEY/AUTH_TOKEN explicitly unset so the
OAuth path is used. Codex: the CLI embedded in the desktop app bundle.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import shutil
import subprocess
from pathlib import Path
from typing import AsyncIterator

CODEX_BIN = "/Applications/ChatGPT.app/Contents/Resources/codex"
STDERR_CAP = 4096   # bounded: never let a runaway/hostile subprocess fill memory or logs

_claude_path: str | None = None


def claude_binary() -> str | None:
    global _claude_path
    if _claude_path is None:
        try:
            if os.name == "nt":
                _claude_path = shutil.which("claude") or shutil.which("claude.exe") or ""
                return _claude_path or None
            out = subprocess.run(["zsh", "-lc", "whence -p claude"],
                                 capture_output=True, text=True, timeout=10).stdout.strip()
            _claude_path = out or shutil.which("claude") or ""
        except (subprocess.SubprocessError, OSError):
            _claude_path = shutil.which("claude") or ""
    return _claude_path or None


def codex_binary() -> str | None:
    """Resolve native codex.exe on Windows, retaining the macOS app fallback."""
    if os.name == "nt":
        return shutil.which("codex") or shutil.which("codex.exe")
    if Path(CODEX_BIN).exists():
        return CODEX_BIN
    return shutil.which("codex")


def _clean_env() -> dict[str, str]:
    env = dict(os.environ)
    env.pop("ANTHROPIC_API_KEY", None)
    env.pop("ANTHROPIC_AUTH_TOKEN", None)
    return env


async def _reap(proc: asyncio.subprocess.Process) -> None:
    """Call from a finally: no child should survive its caller -- timeout,
    cancellation, or any other exit path (issue #16)."""
    if proc.returncode is not None:
        return
    if os.name == "nt":
        # /T terminates descendants too; provider CLIs commonly launch shell
        # and tool children which otherwise survive cancellation or timeout.
        with contextlib.suppress(Exception):
            killer = await asyncio.create_subprocess_exec(
                "taskkill", "/PID", str(proc.pid), "/T", "/F",
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(killer.wait(), timeout=5)
    else:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
    with contextlib.suppress(ProcessLookupError):
        proc.kill()  # fallback if tree/group termination raced
    with contextlib.suppress(Exception):
        await asyncio.wait_for(proc.wait(), timeout=5)


async def _drain_stderr(stream: asyncio.StreamReader | None) -> bytes:
    """Concurrently drain stderr while stdout is read line-by-line, so a
    chatty child can't deadlock on a full stderr pipe. Keeps only the first
    STDERR_CAP bytes; the rest is read (to keep draining) and discarded."""
    if stream is None:
        return b""
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await stream.read(4096)
        if not chunk:
            break
        if total < STDERR_CAP:
            take = chunk[:STDERR_CAP - total]
            chunks.append(take)
            total += len(take)
    return b"".join(chunks)


def _spawn_options() -> dict[str, object]:
    """Put each provider invocation in a separately terminable process tree."""
    if os.name == "nt":
        return {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)}
    return {"start_new_session": True}


class ClaudeCLI:
    name = "claude"

    def available(self) -> str | None:
        return None if claude_binary() else "claude binary not found"

    async def run_text(self, prompt: str, *, model: str = "", timeout: float = 30,
                       cwd: str | None = None, allowed_tools: str | None = None) -> str:
        binary = claude_binary()
        if not binary:
            raise RuntimeError("claude binary not found")
        cmd = [binary, "-p"]
        if model:
            cmd += ["--model", model]
        if allowed_tools:
            cmd += ["--allowedTools", allowed_tools]
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, env=_clean_env(), cwd=cwd, **_spawn_options())
        try:
            try:
                out, err = await asyncio.wait_for(
                    proc.communicate(prompt.encode()), timeout=timeout)
            except asyncio.TimeoutError:
                raise RuntimeError(f"claude -p timed out after {timeout}s")
            if proc.returncode != 0:
                raise RuntimeError(
                    f"claude -p failed: {err[:STDERR_CAP].decode(errors='replace')[:300]}")
            return out.decode().strip()
        finally:
            await _reap(proc)

    async def stream(self, prompt: str, *, model: str = "",
                     cwd: str | None = None, allowed_tools: str | None = None
                     ) -> AsyncIterator[str]:
        binary = claude_binary()
        if not binary:
            raise RuntimeError("claude binary not found")
        cmd = [binary, "-p", "--output-format", "stream-json",
               "--include-partial-messages", "--verbose"]
        if model:
            cmd += ["--model", model]
        if allowed_tools:
            cmd += ["--allowedTools", allowed_tools]
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, env=_clean_env(), cwd=cwd, **_spawn_options())
        stderr_task = asyncio.create_task(_drain_stderr(proc.stderr))
        try:
            proc.stdin.write(prompt.encode())
            proc.stdin.write_eof()
            assert proc.stdout is not None
            async for raw in proc.stdout:
                try:
                    obj = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                # partial text deltas arrive as wrapped SSE stream events
                if obj.get("type") == "stream_event":
                    ev = obj.get("event") or {}
                    delta = ev.get("delta") or {}
                    if delta.get("type") == "text_delta" and delta.get("text"):
                        yield delta["text"]
            await proc.wait()
            if proc.returncode != 0:
                err = await stderr_task
                raise RuntimeError(f"claude -p failed: {err.decode(errors='replace')[:300]}")
        finally:
            await _reap(proc)
            stderr_task.cancel()
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await stderr_task


class CodexCLI:
    name = "codex"

    def available(self) -> str | None:
        if not codex_binary():
            return "codex binary not found"
        auth = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "auth.json"
        if not auth.exists():
            return f"{auth} missing (not logged in)"
        return None

    async def run_text(self, prompt: str, *, model: str = "", timeout: float = 60,
                       cwd: str | None = None, allowed_tools: str | None = None) -> str:
        chunks = []
        async with asyncio.timeout(timeout):
            async for chunk in self.stream(prompt, model=model, cwd=cwd):
                chunks.append(chunk)
        return "".join(chunks).strip()

    async def stream(self, prompt: str, *, model: str = "",
                     cwd: str | None = None, allowed_tools: str | None = None
                     ) -> AsyncIterator[str]:
        binary = codex_binary()
        if not binary:
            raise RuntimeError("codex binary not found")
        cmd = [binary, "exec", "--json", "--skip-git-repo-check"]
        if model:
            cmd += ["--model", model]
        cmd.append(prompt)
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, cwd=cwd, **_spawn_options())
        stderr_task = asyncio.create_task(_drain_stderr(proc.stderr))
        emitted_message = False
        try:
            assert proc.stdout is not None
            async for raw in proc.stdout:
                try:
                    obj = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                # codex exec --json emits JSONL events; surface agent text deltas/messages
                t = obj.get("type", "")
                if t in ("item.completed", "item.updated"):
                    item = obj.get("item") or {}
                    if item.get("type") == "agent_message" and item.get("text"):
                        if emitted_message and not item["text"].startswith("\n"):
                            yield "\n"
                        yield item["text"]
                        emitted_message = True
                elif t == "agent_message" and obj.get("message"):
                    if emitted_message and not obj["message"].startswith("\n"):
                        yield "\n"
                    yield obj["message"]
                    emitted_message = True
                elif t == "agent_message_delta" and obj.get("delta"):
                    yield obj["delta"]
            await proc.wait()
            if proc.returncode != 0:
                err = await stderr_task
                raise RuntimeError(f"codex exec failed: {err.decode(errors='replace')[:300]}")
        finally:
            await _reap(proc)
            stderr_task.cancel()
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await stderr_task


PROVIDERS = {"claude": ClaudeCLI(), "codex": CodexCLI()}


def get_provider(name: str):
    return PROVIDERS.get(name, PROVIDERS["claude"])
