"""LLM providers via headless CLIs — no SDK, no API keys.

Claude: `claude -p` resolved past the user's shell alias; inherits settings env
(local proxy, Max OAuth). ANTHROPIC_API_KEY/AUTH_TOKEN explicitly unset so the
OAuth path is used. Codex: the CLI embedded in the desktop app bundle.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import AsyncIterator

CODEX_BIN = "/Applications/ChatGPT.app/Contents/Resources/codex"

_claude_path: str | None = None


def claude_binary() -> str | None:
    global _claude_path
    if _claude_path is None:
        try:
            out = subprocess.run(["zsh", "-lc", "whence -p claude"],
                                 capture_output=True, text=True, timeout=10).stdout.strip()
            _claude_path = out or shutil.which("claude") or ""
        except (subprocess.SubprocessError, OSError):
            _claude_path = shutil.which("claude") or ""
    return _claude_path or None


def _clean_env() -> dict[str, str]:
    env = dict(os.environ)
    env.pop("ANTHROPIC_API_KEY", None)
    env.pop("ANTHROPIC_AUTH_TOKEN", None)
    return env


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
            stderr=asyncio.subprocess.PIPE, env=_clean_env(), cwd=cwd)
        try:
            out, err = await asyncio.wait_for(
                proc.communicate(prompt.encode()), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            raise RuntimeError(f"claude -p timed out after {timeout}s")
        if proc.returncode != 0:
            raise RuntimeError(f"claude -p failed: {err.decode()[:300]}")
        return out.decode().strip()

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
            stderr=asyncio.subprocess.DEVNULL, env=_clean_env(), cwd=cwd)
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


class CodexCLI:
    name = "codex"

    def available(self) -> str | None:
        if not Path(CODEX_BIN).exists():
            return "embedded codex binary not found"
        if not (Path.home() / ".codex" / "auth.json").exists():
            return "~/.codex/auth.json missing (not logged in)"
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
        cmd = [CODEX_BIN, "exec", "--json", "--skip-git-repo-check"]
        if model:
            cmd += ["--model", model]
        cmd.append(prompt)
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, cwd=cwd)
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
                    yield item["text"]
            elif t == "agent_message" and obj.get("message"):
                yield obj["message"]
            elif t == "agent_message_delta" and obj.get("delta"):
                yield obj["delta"]
        await proc.wait()


PROVIDERS = {"claude": ClaudeCLI(), "codex": CodexCLI()}


def get_provider(name: str):
    return PROVIDERS.get(name, PROVIDERS["claude"])
