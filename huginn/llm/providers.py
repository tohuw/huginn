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

from ..plugins import LLMProviderError

CODEX_BIN = "/Applications/ChatGPT.app/Contents/Resources/codex"
STDERR_CAP = 4096   # bounded: never let a runaway/hostile subprocess fill memory or logs

_claude_path: str | None = None
_internal_pids: set[int] = set()


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
    env["HUGINN_INTERNAL"] = "1"
    return env


def is_internal_pid(pid: int | None) -> bool:
    """Self-owned guard supplementing Claude's upstream entrypoint field."""
    return pid is not None and pid in _internal_pids


def _track_internal(proc: asyncio.subprocess.Process) -> None:
    if proc.pid is not None:
        _internal_pids.add(proc.pid)


async def _reap(proc: asyncio.subprocess.Process) -> None:
    """Call from a finally: no child should survive its caller -- timeout,
    cancellation, or any other exit path (issue #16)."""
    if proc.returncode is not None:
        _internal_pids.discard(proc.pid)
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
    _internal_pids.discard(proc.pid)


async def _drain_stderr(stream: asyncio.StreamReader | None) -> bytes:
    """Concurrently drain stderr while stdout is read line-by-line, so a
    chatty child can't deadlock on a full stderr pipe. Keeps only the first
    STDERR_CAP bytes; the rest is read (to keep draining) and discarded."""
    if stream is None:
        return b""
    tail = b""
    while True:
        chunk = await stream.read(4096)
        if not chunk:
            break
        tail = (tail + chunk)[-STDERR_CAP:]
    return tail


def _spawn_options() -> dict[str, object]:
    """Put each provider invocation in a separately terminable process tree.

    ``CREATE_NO_WINDOW`` is not optional here, and leaving it out is not a
    cosmetic defect. The daemon runs under ``pythonw`` so that nothing appears
    at login, which means it has no console for a child to inherit; the
    provider CLIs are console-subsystem programs, so Windows gave each one a
    console of its own. Every blurb and every Ask popped a terminal window in
    the user's face, and a batch of them arrived as dozens at once.

    ``CREATE_NEW_PROCESS_GROUP`` does not imply it -- it only controls signal
    delivery -- so both flags are set.
    """
    if os.name == "nt":
        return {"creationflags": (
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
            | getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        )}
    return {"start_new_session": True}


class ClaudeCLI:
    name = "claude"
    default_blurb_model = "haiku"

    def available(self) -> str | None:
        return None if claude_binary() else "claude binary not found"

    async def run_text(self, prompt: str, *, model: str = "", timeout: float = 30,
                       cwd: str | None = None, allowed_tools: str | None = None) -> str:
        binary = claude_binary()
        if not binary:
            raise LLMProviderError("claude binary not found", retryable=False)
        cmd = [binary, "-p", "--no-session-persistence"]
        if model:
            cmd += ["--model", model]
        if allowed_tools:
            cmd += ["--allowedTools", allowed_tools]
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, env=_clean_env(), cwd=cwd, **_spawn_options())
        _track_internal(proc)
        try:
            try:
                out, err = await asyncio.wait_for(
                    proc.communicate(prompt.encode()), timeout=timeout)
            except asyncio.TimeoutError:
                raise RuntimeError(f"claude -p timed out after {timeout}s")
            if proc.returncode != 0:
                detail = err[:STDERR_CAP].decode(errors="replace")[:300]
                permanent = any(marker in detail.lower() for marker in (
                    "model_not_found", "invalid model", "not logged in",
                    "authentication", "unauthorized",
                ))
                raise LLMProviderError(
                    f"claude -p failed: {detail}", retryable=not permanent)
            return out.decode().strip()
        finally:
            await _reap(proc)

    async def stream(self, prompt: str, *, model: str = "",
                     cwd: str | None = None, allowed_tools: str | None = None
                     ) -> AsyncIterator[str]:
        binary = claude_binary()
        if not binary:
            raise LLMProviderError("claude binary not found", retryable=False)
        cmd = [binary, "-p", "--no-session-persistence", "--output-format", "stream-json",
               "--include-partial-messages", "--verbose"]
        if model:
            cmd += ["--model", model]
        if allowed_tools:
            cmd += ["--allowedTools", allowed_tools]
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, env=_clean_env(), cwd=cwd, **_spawn_options())
        _track_internal(proc)
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
                detail = err.decode(errors="replace")[:300]
                permanent = any(marker in detail.lower() for marker in (
                    "model_not_found", "invalid model", "not logged in",
                    "authentication", "unauthorized",
                ))
                raise LLMProviderError(
                    f"claude -p failed: {detail}", retryable=not permanent)
        finally:
            await _reap(proc)
            stderr_task.cancel()
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await stderr_task


class CodexCLI:
    name = "codex"
    default_blurb_model = ""

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
            stderr=asyncio.subprocess.PIPE, env=_clean_env(), cwd=cwd, **_spawn_options())
        _track_internal(proc)
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


BUILTIN_PROVIDERS = {"claude": ClaudeCLI(), "codex": CodexCLI()}
# Compatibility alias for code that imported the original registry directly.
PROVIDERS = BUILTIN_PROVIDERS


def all_providers(registry=None):
    """Built-ins plus installed plugin providers, with built-ins reserved."""
    from ..plugins import get_registry

    result = dict(BUILTIN_PROVIDERS)
    for name, provider in (registry or get_registry()).providers().items():
        if name not in result:
            result[name] = provider
    return result


def get_provider(name: str, registry=None):
    """The provider registered under ``name``, or None when nothing is.

    Returns None rather than falling back, and callers must refuse -- issue #41
    C2. This used to return ``BUILTIN_PROVIDERS["claude"]`` for *any* unknown
    name, so ``policy.check(model, "bedrock")`` validated the string "bedrock"
    against a ``require_provider="bedrock"`` policy, passed, and then dispatched
    to ``ClaudeCLI``. An absent or API-mismatched plugin therefore meant the
    gate approved one provider while a different one ran -- reachable by
    accident, and more likely now that #38 ships API ranges and a mismatched
    plugin stays installed while contributing nothing.

    The registered object's own ``name`` must agree with the key it was looked
    up under, so the gate and the dispatch cannot disagree even if some future
    registry path keys a provider under the wrong name.
    """
    selected = all_providers(registry).get(name)
    if selected is None:
        return None
    actual = str(getattr(selected, "name", "") or "")
    if actual and actual != name:
        return None
    return selected


def effective_provider_name(provider: object, requested: str) -> str:
    """The name to gate on: the resolved provider's own, not the caller's string.

    issue #41 C2: the policy verdict has to describe the object that will
    actually run. ``get_provider`` already refuses a name/object mismatch, so
    these agree in practice; asking the object keeps it true if that ever
    changes. Falls back to ``requested`` only for a provider exposing no name
    at all, which the plugin contract forbids.
    """
    return str(getattr(provider, "name", "") or "") or requested


def compatible_model(provider: str, model: str, registry=None) -> str:
    """Avoid carrying one provider's configured model into the other CLI."""
    value = (model or "").strip()
    if not value:
        return ""
    selected = get_provider(provider, registry)
    if selected is None:
        # No installed provider by that name, so no model is compatible with
        # it. Returning the value unchanged would hand a model to whatever ran
        # instead -- the C2 substitution this refuses to make.
        return ""
    plugin_filter = getattr(selected, "compatible_model", None)
    if callable(plugin_filter):
        return str(plugin_filter(value) or "")
    lowered = value.lower()
    is_claude = lowered.startswith("claude") or lowered in {"haiku", "sonnet", "opus"}
    return value if (provider == "claude") == is_claude else ""


def blurb_model(provider: str, model: str, registry=None) -> str:
    """Resolve a cheap provider-specific model for automatic card text.

    Providers may implement ``resolve_blurb_model(configured)`` when model
    identifiers differ across backends (for example, Anthropic API names
    versus Bedrock inference-profile IDs). Built-ins retain their existing
    compatibility behavior while preferring Claude's stable ``haiku`` alias.
    """
    selected = get_provider(provider, registry)
    if selected is None:
        # issue #41 C2: no silent fallback to Claude's "haiku". A name nothing
        # is registered under is a configuration fault, and permanent.
        raise LLMProviderError(
            f"no installed provider named {provider!r}", retryable=False)
    configured = (model or "").strip()
    resolver = getattr(selected, "resolve_blurb_model", None)
    if callable(resolver):
        resolved = str(resolver(configured) or "").strip()
        if not resolved:
            raise LLMProviderError(
                f"{provider} has no compatible automatic-title model",
                retryable=False,
            )
        return resolved
    compatible = compatible_model(provider, configured, registry)
    if compatible:
        return compatible
    default = str(getattr(selected, "default_blurb_model", "") or "").strip()
    if configured and provider == "claude":
        raise LLMProviderError(
            "configured automatic-title model is incompatible with Claude",
            retryable=False,
        )
    return default
