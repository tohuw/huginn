"""Installed Huginn plugin discovery and the public plugin contract.

Plugins are ordinary, trusted Python distributions registered through the
``huginn.plugins`` package-metadata entry-point group.  Huginn deliberately
does not scan arbitrary source directories: installing a distribution is the
explicit trust decision that permits its code to run in the local daemon.
"""
from __future__ import annotations

import re
import time
import logging
from dataclasses import dataclass, field
from functools import lru_cache
from importlib import metadata
from typing import TYPE_CHECKING, Any, Protocol

from .model import Event, Session

if TYPE_CHECKING:
    from .bus import Bus
    from .config import Config
    from .diagnostics import Diagnostics


API_VERSION = 1
ENTRY_POINT_GROUP = "huginn.plugins"
_NAME_RE = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$")
_EXTERNAL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$")
MAX_SOURCE_SUMMARY_CHARS = 4000
_RESERVED_PROVIDER_NAMES = frozenset({"claude", "codex"})
LOG = logging.getLogger("huginn.plugins")


class LLMProvider(Protocol):
    """Provider capability contributed by a plugin."""

    name: str

    def available(self) -> str | None: ...

    async def run_text(
        self,
        prompt: str,
        *,
        model: str = "",
        timeout: float = 30,
        cwd: str | None = None,
        allowed_tools: str | None = None,
    ) -> str: ...

    def stream(
        self,
        prompt: str,
        *,
        model: str = "",
        cwd: str | None = None,
        allowed_tools: str | None = None,
    ) -> Any: ...


class SessionSource(Protocol):
    """Long-running source capability contributed by a plugin."""

    name: str

    async def run(self, context: "SourceContext") -> None: ...


@dataclass(frozen=True)
class PluginSpec:
    """Stable object returned by every ``huginn.plugins`` entry point."""

    name: str
    version: str
    api_version: int = API_VERSION
    providers: tuple[LLMProvider, ...] = ()
    sources: tuple[SessionSource, ...] = ()


@dataclass(frozen=True)
class PluginLoadError:
    entry_point: str
    error_class: str
    detail: str

    def to_dict(self) -> dict[str, str]:
        return {
            "entry_point": self.entry_point,
            "error_class": self.error_class,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class PluginRegistry:
    plugins: tuple[PluginSpec, ...] = ()
    errors: tuple[PluginLoadError, ...] = ()

    def providers(self) -> dict[str, LLMProvider]:
        result: dict[str, LLMProvider] = {}
        for plugin in self.plugins:
            for provider in plugin.providers:
                result[provider.name] = provider
        return result

    def sources(self) -> tuple[tuple[PluginSpec, SessionSource], ...]:
        return tuple(
            (plugin, source)
            for plugin in self.plugins
            for source in plugin.sources
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "api_version": API_VERSION,
            "plugins": [
                {
                    "name": plugin.name,
                    "version": plugin.version,
                    "providers": [provider.name for provider in plugin.providers],
                    "sources": [source.name for source in plugin.sources],
                }
                for plugin in self.plugins
            ],
            "errors": [error.to_dict() for error in self.errors],
        }


def _invalid_name(kind: str, value: Any) -> ValueError | None:
    if not isinstance(value, str) or not _NAME_RE.fullmatch(value):
        return ValueError(
            f"{kind} name must match {_NAME_RE.pattern}: {value!r}"
        )
    return None


def _validate_plugin(plugin: Any) -> PluginSpec:
    if not isinstance(plugin, PluginSpec):
        raise TypeError("entry point must return huginn.plugins.PluginSpec")
    if plugin.api_version != API_VERSION:
        raise ValueError(
            f"plugin API {plugin.api_version} is incompatible with Huginn API {API_VERSION}"
        )
    invalid = _invalid_name("plugin", plugin.name)
    if invalid:
        raise invalid
    if not isinstance(plugin.version, str) or not plugin.version.strip():
        raise ValueError("plugin version must be a non-empty string")
    seen_providers: set[str] = set()
    for provider in plugin.providers:
        invalid = _invalid_name("provider", getattr(provider, "name", None))
        if invalid:
            raise invalid
        if provider.name in seen_providers:
            raise ValueError(f"duplicate provider in plugin: {provider.name}")
        if provider.name in _RESERVED_PROVIDER_NAMES:
            raise ValueError(f"provider name is reserved by Huginn: {provider.name}")
        seen_providers.add(provider.name)
    seen_sources: set[str] = set()
    for source in plugin.sources:
        invalid = _invalid_name("source", getattr(source, "name", None))
        if invalid:
            raise invalid
        if source.name in seen_sources:
            raise ValueError(f"duplicate source in plugin: {source.name}")
        seen_sources.add(source.name)
    return plugin


def discover_plugins(entry_points: Any = None) -> PluginRegistry:
    """Load installed plugins, isolating one broken distribution from others."""
    selected = (
        metadata.entry_points(group=ENTRY_POINT_GROUP)
        if entry_points is None
        else entry_points
    )
    plugins: list[PluginSpec] = []
    errors: list[PluginLoadError] = []
    plugin_names: set[str] = set()
    provider_names: set[str] = set()
    source_names: set[str] = set()
    for entry_point in sorted(selected, key=lambda item: item.name):
        try:
            loaded = entry_point.load()
            candidate = loaded() if callable(loaded) and not isinstance(loaded, PluginSpec) else loaded
            plugin = _validate_plugin(candidate)
            if plugin.name in plugin_names:
                raise ValueError(f"duplicate plugin name: {plugin.name}")
            for provider in plugin.providers:
                if provider.name in provider_names:
                    raise ValueError(f"duplicate provider name: {provider.name}")
            for source in plugin.sources:
                if source.name in source_names:
                    raise ValueError(f"duplicate source name: {source.name}")
            plugins.append(plugin)
            plugin_names.add(plugin.name)
            provider_names.update(provider.name for provider in plugin.providers)
            source_names.update(source.name for source in plugin.sources)
        except Exception as exc:
            # The API gets core validation/import details only. An arbitrary
            # plugin exception may contain credentials or payload data, so its
            # message stays in the local daemon log rather than the API.
            LOG.error(
                "plugin entry point %s failed to load (%s)",
                entry_point.name,
                type(exc).__name__,
            )
            safe_detail = (
                str(exc).replace("\n", " ")[:300]
                if isinstance(exc, (ImportError, TypeError, ValueError))
                else "plugin failed to load; inspect the local daemon log"
            )
            errors.append(PluginLoadError(
                entry_point=str(entry_point.name)[:120],
                error_class=type(exc).__name__,
                detail=safe_detail,
            ))
    return PluginRegistry(tuple(plugins), tuple(errors))


@lru_cache(maxsize=1)
def get_registry() -> PluginRegistry:
    return discover_plugins()


def clear_registry_cache() -> None:
    """Refresh installed-distribution discovery (primarily for tests/tools)."""
    get_registry.cache_clear()


@dataclass(frozen=True)
class SourceContext:
    """Narrow daemon capability passed to an installed session source."""

    plugin_name: str
    source_name: str
    config: "Config"
    bus: "Bus" = field(repr=False)
    diagnostics: "Diagnostics" = field(repr=False)

    @property
    def namespace(self) -> str:
        return f"{self.plugin_name}.{self.source_name}"

    @property
    def diagnostic_name(self) -> str:
        return f"plugin.{self.namespace}"

    def key(self, external_id: str) -> str:
        """Build a collision-proof session key from a bounded external ID."""
        if not isinstance(external_id, str) or not _EXTERNAL_ID_RE.fullmatch(external_id):
            raise ValueError("plugin session IDs must be 1-160 safe characters")
        return f"plugin:{self.namespace}:{external_id}"

    def upsert(self, session: Session) -> None:
        expected = f"plugin:{self.namespace}:"
        if not isinstance(session, Session) or not session.key.startswith(expected):
            raise ValueError(f"plugin session key must start with {expected}")
        if session.source != self.source_name:
            raise ValueError(f"plugin session source must be {self.source_name}")
        if session.source_summary is not None:
            if not isinstance(session.source_summary, str):
                raise ValueError("plugin source summary must be text")
            if len(session.source_summary) > MAX_SOURCE_SUMMARY_CHARS:
                raise ValueError(
                    f"plugin source summary is limited to {MAX_SOURCE_SUMMARY_CHARS} characters"
                )
        self.bus.emit(Event(
            "plugin.session",
            session.key,
            time.time(),
            self.diagnostic_name,
            {"session": session},
        ))

    def remove(self, key: str) -> None:
        expected = f"plugin:{self.namespace}:"
        if not isinstance(key, str) or not key.startswith(expected):
            raise ValueError(f"plugin session key must start with {expected}")
        self.bus.emit(Event(
            "plugin.remove",
            key,
            time.time(),
            self.diagnostic_name,
        ))

    def ok(self) -> None:
        self.diagnostics.ok(self.diagnostic_name)

    def error(self, exc: BaseException) -> None:
        self.diagnostics.error(self.diagnostic_name, exc)


__all__ = [
    "API_VERSION",
    "ENTRY_POINT_GROUP",
    "LLMProvider",
    "MAX_SOURCE_SUMMARY_CHARS",
    "PluginRegistry",
    "PluginSpec",
    "SessionSource",
    "SourceContext",
    "clear_registry_cache",
    "discover_plugins",
    "get_registry",
]
