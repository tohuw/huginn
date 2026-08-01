"""Installed Huginn plugin discovery and the public plugin contract.

Plugins are ordinary, trusted Python distributions registered through the
``huginn.plugins`` package-metadata entry-point group.  Huginn deliberately
does not scan arbitrary source directories: installing a distribution is the
explicit trust decision that permits its code to run in the local daemon.

This registry is purely additive: a plugin contributes capabilities and cannot
veto another's. Restricting which models may be used is therefore not
expressible here -- see ``huginn.policy`` for that chokepoint (issue #41).
"""
from __future__ import annotations

import re
import time
import logging
from dataclasses import dataclass, field
from functools import lru_cache
from importlib import metadata
from typing import TYPE_CHECKING, Any, Callable, Protocol

from .model import Event, Session

if TYPE_CHECKING:
    from .bus import Bus
    from .config import Config
    from .diagnostics import Diagnostics


API_VERSION = 1
# Oldest plugin API core still speaks. Core advertises the inclusive range
# MIN_API_VERSION..API_VERSION and accepts any plugin whose own declared range
# overlaps it -- issue #38: an exact `!=` comparison meant a routine
# API_VERSION bump silently disabled every installed plugin. Widening the
# accepted range is a one-line change here; a genuinely breaking change raises
# MIN_API_VERSION and then a stale plugin is refused loudly, on purpose.
MIN_API_VERSION = 1
# Most a plugin may declare for min_api/max_api/api_version -- issue #41 M1.
# Deliberately generous rather than API_VERSION: declaring the next few
# versions is issue #38's entire point, so clamping to API_VERSION would both
# refuse the supported case and discard what api_range publishes. This only
# rules out a number no plugin author could mean, like max_api=10**9.
MAX_DECLARABLE_API = API_VERSION + 100
ENTRY_POINT_GROUP = "huginn.plugins"
_NAME_RE = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$")
_EXTERNAL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$")
MAX_SOURCE_SUMMARY_CHARS = 4000
MAX_GROUP_LABEL_CHARS = 60
MAX_SOURCE_LABEL_CHARS = 40
_RESERVED_PROVIDER_NAMES = frozenset({"claude", "codex"})
LOG = logging.getLogger("huginn.plugins")


class LLMProviderError(RuntimeError):
    """Provider failure with an explicit retry contract.

    Plugin providers may raise this error, or any exception carrying a
    ``retryable`` boolean attribute. Background workers can then distinguish
    transient service trouble from configuration/authentication failures
    without inspecting error-message text.
    """

    def __init__(self, message: str, *, retryable: bool = True):
        super().__init__(message)
        self.retryable = retryable


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
    """Stable object returned by every ``huginn.plugins`` entry point.

    A plugin may declare the inclusive range of plugin APIs it supports with
    ``min_api``/``max_api``; both default to ``api_version``, so a spec that
    only sets ``api_version`` behaves exactly as it did before issue #38 --
    accepted when the versions match, refused when they do not. Declaring a
    range is what lets a plugin survive a core ``API_VERSION`` bump instead of
    silently disappearing from the daemon's view.

    ``min_api``/``max_api`` are declared last so existing positional
    construction (``PluginSpec(name, version, api_version, providers,
    sources)``) keeps working unchanged.
    """

    name: str
    version: str
    api_version: int = API_VERSION
    providers: tuple[LLMProvider, ...] = ()
    sources: tuple[SessionSource, ...] = ()
    min_api: int | None = None
    max_api: int | None = None

    @property
    def api_range(self) -> tuple[int, int]:
        """The inclusive plugin-API range this spec supports."""
        low = self.api_version if self.min_api is None else self.min_api
        high = self.api_version if self.max_api is None else self.max_api
        return (low, high)


class PluginApiMismatch(ValueError):
    """A plugin whose supported API range does not overlap core's.

    Its own exception type (rather than a bare ``ValueError``) so
    ``discover_plugins`` can log the specific version numbers and
    ``huginn doctor`` can label it as an API mismatch instead of an
    indistinguishable validation failure -- issue #38's "surface it loudly".
    """


@dataclass(frozen=True)
class PluginLoadError:
    entry_point: str
    error_class: str
    detail: str
    # issue #38: an API-range mismatch is not an ordinary broken plugin -- it
    # is core and the plugin disagreeing about the contract, and it is the one
    # failure a routine core version bump can cause across every installed
    # plugin at once. Flagged so `huginn doctor` and GET /api/plugins can name
    # it as such instead of burying it among import failures.
    api_mismatch: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "entry_point": self.entry_point,
            "error_class": self.error_class,
            "detail": self.detail,
            "api_mismatch": self.api_mismatch,
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

    def api_mismatches(self) -> tuple[PluginLoadError, ...]:
        return tuple(error for error in self.errors if error.api_mismatch)

    def to_dict(self) -> dict[str, Any]:
        return {
            "api_version": API_VERSION,
            "min_api_version": MIN_API_VERSION,
            "plugins": [
                {
                    "name": plugin.name,
                    "version": plugin.version,
                    "api_range": list(plugin.api_range),
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


def _api_ranges_overlap(plugin: PluginSpec) -> bool:
    """Two inclusive ranges overlap unless one ends before the other begins."""
    low, high = plugin.api_range
    return low <= API_VERSION and high >= MIN_API_VERSION


def _validate_api_int(label: str, value: Any) -> int:
    """One API version number, or ``PluginApiMismatch`` -- issue #41 M1.

    The overlap arithmetic itself is correct; what was missing was any
    type/bound check on its inputs. ``min_api=-999``, ``max_api=10**9`` and
    ``min_api=True`` (``bool`` is an ``int`` subclass, so it silently means 1)
    all loaded, and a non-int ``api_version`` raised a bare ``TypeError`` --
    so ``doctor`` and ``GET /api/plugins`` labelled a version disagreement as
    an indistinguishable load failure with ``api_mismatch`` False, which is
    exactly the mislabelling issue #38 exists to prevent.

    ``PluginApiMismatch`` rather than ``ValueError`` for the same reason: this
    *is* core and the plugin disagreeing about the contract, so ``doctor`` and
    ``GET /api/plugins`` must be able to say so.

    ``bool`` is rejected explicitly: it is an ``int`` subclass, so ``min_api=True``
    silently meant 1 -- accidentally valid, and never what anyone typed on purpose.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise PluginApiMismatch(
            f"{label} must be an integer, not {type(value).__name__}: {value!r}")
    if value < 0:
        raise PluginApiMismatch(f"{label} must not be negative: {value}")
    if value > MAX_DECLARABLE_API:
        # A plugin cannot know an API it has never seen. Declaring the next few
        # versions is issue #38's whole point and stays supported; max_api=10**9
        # is different in kind -- it asserts forward compatibility with every
        # contract that will ever exist, so a genuinely breaking change could
        # never disable the plugin, which is what raising MIN_API_VERSION is for.
        raise PluginApiMismatch(
            f"{label} is {value}, beyond any API this Huginn could describe "
            f"(core is at {API_VERSION}, and {MAX_DECLARABLE_API} is the most a "
            "plugin may claim); declare the versions you actually support"
        )
    return value


def _validate_plugin(plugin: Any) -> PluginSpec:
    if not isinstance(plugin, PluginSpec):
        raise TypeError("entry point must return huginn.plugins.PluginSpec")
    _validate_api_int("api_version", plugin.api_version)
    for label, value in (("min_api", plugin.min_api), ("max_api", plugin.max_api)):
        if value is not None:
            _validate_api_int(label, value)
    low, high = plugin.api_range
    if low > high:
        raise PluginApiMismatch(
            f"plugin API range {low}..{high} is inverted (min_api exceeds max_api)"
        )
    if not _api_ranges_overlap(plugin):
        raise PluginApiMismatch(
            f"plugin supports API {low}..{high}, which is incompatible with "
            f"Huginn API {MIN_API_VERSION}..{API_VERSION}"
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
            if isinstance(exc, PluginApiMismatch):
                # issue #38: a version disagreement used to be a quiet skip
                # visible only in `huginn doctor`. It is a WARNING at minimum
                # on every daemon start, naming both ranges, because the usual
                # cause is a core bump that just disabled every plugin at once.
                LOG.warning(
                    "plugin entry point %s disabled: %s (Huginn API %d..%d) -- "
                    "the plugin is still installed but contributes nothing",
                    entry_point.name, exc, MIN_API_VERSION, API_VERSION,
                )
            else:
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
                api_mismatch=isinstance(exc, PluginApiMismatch),
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
    _existing_keys: Callable[[], tuple[str, ...]] = field(
        default=lambda: (), repr=False
    )

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

    def existing_keys(self) -> tuple[str, ...]:
        """Return only this source's restored/current keys for reconciliation."""
        expected = f"plugin:{self.namespace}:"
        return tuple(
            key for key in self._existing_keys()
            if isinstance(key, str) and key.startswith(expected)
        )

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
        if session.group is not None and not _NAME_RE.fullmatch(session.group):
            raise ValueError(f"session group must match {_NAME_RE.pattern}: {session.group!r}")
        if session.group_label is not None:
            if not isinstance(session.group_label, str) or not session.group_label.strip():
                raise ValueError("session group_label must be non-empty text")
            if len(session.group_label) > MAX_GROUP_LABEL_CHARS:
                raise ValueError(
                    f"session group_label is limited to {MAX_GROUP_LABEL_CHARS} characters"
                )
        if session.group_label is not None and session.group is None:
            raise ValueError("session group_label requires group to be set")
        if session.source_label is not None:
            if not isinstance(session.source_label, str) or not session.source_label.strip():
                raise ValueError("session source_label must be non-empty text")
            if len(session.source_label) > MAX_SOURCE_LABEL_CHARS:
                raise ValueError(
                    f"session source_label is limited to {MAX_SOURCE_LABEL_CHARS} characters"
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
    "LLMProviderError",
    "MAX_GROUP_LABEL_CHARS",
    "MAX_SOURCE_LABEL_CHARS",
    "MAX_SOURCE_SUMMARY_CHARS",
    "MIN_API_VERSION",
    "PluginApiMismatch",
    "PluginLoadError",
    "PluginRegistry",
    "PluginSpec",
    "SessionSource",
    "SourceContext",
    "clear_registry_cache",
    "discover_plugins",
    "get_registry",
]
