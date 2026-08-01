"""The model-policy chokepoint: every LLM call Huginn makes routes through here.

Why this cannot be a plugin (issue #41): ``huginn/plugins.py`` is purely
additive. Plugins contribute providers and nothing can veto a contribution --
load order is arbitrary, the registry is ``lru_cache``-memoized, and additions
compose while removals do not. So "inference must route only through our
approved provider" is not expressible as a plugin; enforcement has to be a
chokepoint in core that a distribution pins closed.

Two rules that are not negotiable, because either one being wrong defeats the
point of a restricted contract:

1. **Policies intersect, never union.** A call is permitted only when *every*
   resolved policy permits it. A contributed distribution, a config value, an
   environment variable, or a CLI flag may narrow the allowed set; none of
   them may widen it.
2. **No match means refuse, not fall back.** A (model, provider) pair no
   policy addresses is refused, and the refusing policy's ``reason`` is
   surfaced verbatim. A refused model is never silently swapped for a
   permitted one.

Honest scope: this is a strong contract, not a sandbox. It governs Huginn's
own calls. Anyone with write access to the environment can edit anything, and
nothing here stops a user running any model in a different tool. Its value is
being explicit, testable, and CI-verifiable -- preventing accidental violation
and making drift detectable. See docs/plugins.md, "Model policy".
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from importlib import metadata
from importlib.metadata import entry_points
from typing import Any

POLICY_ENTRY_POINT_GROUP = "huginn.policy"
LOG = logging.getLogger("huginn.policy")
# PEP 503/508 distribution-name normalisation, reimplemented rather than
# imported from importlib.metadata.Prepared, which is private.
_NON_ALPHANUM = re.compile(r"[-_.]+")


def _normalize(name: str) -> str:
    return _NON_ALPHANUM.sub("_", name).lower()


@dataclass(frozen=True)
class ModelPolicy:
    """One installed distribution's statement of which calls it permits.

    ``allow`` patterns are matched with ``re.search``, not ``re.fullmatch``.
    Anchor a pattern with ``^`` (and ``$`` where a suffix matters) to restrict
    a prefix strictly -- ``r"^us\\.anthropic\\."`` and not ``r"us\\.anthropic\\."``,
    since the latter also permits ``evil-us.anthropic.foo``. Unanchored search
    stays the primitive because it lets a policy allowlist a vendor prefix
    embedded in a longer qualified id (for example
    ``bedrock/us.anthropic.claude-...``); it is not a recommendation.

    An empty ``allow`` tuple permits nothing. That is the correct reading, not
    a degenerate one: see ``_load_failed`` below.
    """

    name: str
    allow: tuple[str, ...]                 # regex allowlist of model ids (re.search)
    require_provider: str | None = None    # None = any provider
    reason: str = ""                       # shown verbatim on refusal
    # Compiled eagerly at construction, not a declared field: excluded from
    # __eq__/__repr__ so a policy still compares and prints by its declaration.
    _patterns: tuple[re.Pattern[str], ...] = field(
        default=(), init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        """Reject a malformed ``allow`` at construction rather than at call time.

        Two *independent* guarantees, which is why both are asserted here.

        H1 -- the ``tuple`` check. ``allow`` was only a type *hint* on a frozen
        dataclass that validated nothing, so one missing comma
        (``allow=r"^us-anthropic-"`` for ``allow=(r"^us-anthropic-",)``) made
        ``_permits`` iterate the pattern's *characters*. A lone ``^`` matches
        every model id under ``re.search``, so a policy meant to permit one
        vendor prefix permitted everything, and the empty-``allow`` guard passed
        too because a non-empty string is truthy.

        H2 -- eager ``re.compile``. A bad regex becomes a construction error the
        caller's ``except`` turns into the refuse-everything ``_load_failed``
        policy, instead of escaping ``_permits`` later as a bare ``re.error``
        that 500s ``/api/providers`` and, in ``blurb.py``, lands in a broad
        handler where ``retryable`` defaults True and so retries with backoff
        instead of latching.

        Compilation does **not** backstop H1 and must not be relied on to.
        ``r"^us\\.anthropic\\."`` split into characters happens to yield a lone
        ``"\\"`` that fails to compile, which makes that one example look
        caught; a backslash-free prefix like ``"^us-anthropic-"`` has *every*
        character compile cleanly, and the resulting ``^`` then permits
        everything silently. The explicit ``tuple``/``str`` checks are what
        actually catch this class.
        """
        if isinstance(self.allow, str) or not isinstance(self.allow, tuple):
            raise TypeError(
                "ModelPolicy.allow must be a tuple of regex strings, not "
                f"{type(self.allow).__name__} (a bare string would be matched "
                "character by character)"
            )
        for pattern in self.allow:
            if not isinstance(pattern, str):
                raise TypeError(
                    f"ModelPolicy.allow patterns must be strings, not {type(pattern).__name__}")
        if self.require_provider is not None and not isinstance(self.require_provider, str):
            raise TypeError("ModelPolicy.require_provider must be a string or None")
        if not isinstance(self.reason, str):
            raise TypeError("ModelPolicy.reason must be a string")
        object.__setattr__(
            self, "_patterns", tuple(re.compile(pattern) for pattern in self.allow))


# The permissive default is a real ModelPolicy, not a bypass branch, so the
# unrestricted path (no policy installed -- Huginn's default, unchanged by
# issue #41) exercises exactly the same intersection and fail-closed code a
# restricted build does. A default that were merely the *absence* of a policy
# would mean restricted and unrestricted builds run different code, and the
# restricted path is the one nobody can afford to leave untested in normal use.
DEFAULT_POLICY = ModelPolicy(
    name="default",
    allow=(".*",),
    require_provider=None,
    reason="no restriction configured",
)


class PolicyRefused(RuntimeError):
    """Raised by ``check()`` when at least one resolved policy refuses.

    ``retryable = False`` so the automatic-text circuit breaker treats a
    refusal as permanent configuration, not transient service trouble -- see
    ``huginn.plugins.LLMProviderError`` for that contract.
    """

    retryable = False


def _permits(policy: ModelPolicy, model: str, provider: str) -> bool:
    """Whether one policy permits one call. Any surprise here means refuse.

    Issue #41 H2: matching used to call ``re.search`` on an unvalidated pattern,
    so a malformed regex escaped as ``re.error`` -- not a ``PolicyRefused`` --
    and widened nothing but broke every surface that asks. Patterns are now
    compiled at construction, and this is the belt to that braces: a matching
    fault must never be readable as "permitted".

    ``Exception`` and deliberately *not* ``BaseException``, unlike the load
    guard in ``resolve()``. The asymmetry is the point. At load time a stray
    ``sys.exit()`` in a policy module is the hazard, and swallowing it is
    correct. Here the plausible ``BaseException`` is a ``KeyboardInterrupt``
    arriving during a pathological pattern's backtracking -- precisely when a
    user reaches for Ctrl-C -- and catching that to report a refusal would make
    the call unkillable. A refusal is not worth an uninterruptible process.
    """
    try:
        if policy.require_provider is not None and provider != policy.require_provider:
            return False
        return any(pattern.search(model) for pattern in policy._patterns)
    except Exception:
        LOG.exception("model policy %s failed while matching; refusing the call", policy.name)
        return False


def _load_failed(name: str, exc: BaseException) -> ModelPolicy:
    """A policy entry point that fails to load must refuse, not vanish.

    ``discover_plugins()`` isolates and *skips* a broken plugin, which is
    right there: a missing provider only removes an option. A policy is the
    opposite -- dropping a broken restrictive policy would widen the effective
    permission set, which is the one thing this module exists to prevent. A
    deployment that installed an approved-provider-only policy and then
    shipped a build where that policy fails to import would silently become
    unrestricted. Same word, opposite correct answer.

    Only the exception class name is embedded in the reason: an arbitrary
    exception message may carry credentials or payload data, the same rule
    ``discover_plugins()`` follows.
    """
    return ModelPolicy(
        name=f"{name}(load-error)",
        allow=(),
        require_provider=None,
        reason=f"policy entry point {name!r} failed to load: {type(exc).__name__}",
    )


def _declares_policy_unparseably(distribution: Any) -> bool:
    """Whether ``entry_points.txt`` mentions our group but parsed to nothing.

    Issue #41 C1's other route: a corrupt or truncated ``entry_points.txt`` (a
    missing ``]``, say) makes ``importlib`` yield zero entry points *silently*,
    so a restrictive policy that is installed and declared reads exactly like no
    policy at all. A discovery failure must never widen the permitted set.
    """
    try:
        raw = distribution.read_text("entry_points.txt") or ""
    except Exception:
        return False
    return POLICY_ENTRY_POINT_GROUP in raw


class _CorruptDeclaration:
    """Stand-in entry point whose ``load()`` always fails, so it always refuses.

    Reuses the ``_load_failed`` path rather than inventing a second refusal
    shape: from ``resolve()``'s perspective a declaration it cannot parse and a
    policy it cannot import are the same fact.
    """

    value = "(unparseable entry_points.txt)"

    def __init__(self, distribution_name: str):
        self.name = f"{distribution_name}(unparseable-declaration)"

    def load(self) -> ModelPolicy:
        raise ValueError("entry_points.txt names huginn.policy but could not be parsed")


def _distribution_entry_points() -> tuple[list[Any], tuple[str, ...]]:
    """Policy entry points found by walking every installed distribution.

    Issue #41 C1: ``importlib.metadata.entry_points()`` dedupes distributions
    by *normalised name, first on sys.path wins*. A directory earlier on
    ``sys.path`` containing nothing but ``mypol-9.9.dist-info/METADATA`` -- same
    name, no ``entry_points.txt`` -- therefore masks the real ``mypol`` dist,
    and the group query returns zero entry points. ``resolve()`` cannot
    distinguish that from "no policy is installed", so it fell back to the
    permissive ``DEFAULT_POLICY`` and the excluded model became usable again.
    ``huginn serve`` runs with CWD on ``sys.path[0]`` and ``agent_install``'s
    ``WorkingDirectory`` is ``REPO_ROOT``, so a writable checkout is enough.

    Walking ``distributions()`` ourselves sees every dist-info on the path,
    shadowed or not, and finds the policy the masking dist hides.

    Returns the entry points plus the normalised distribution names that
    contributed a policy more than once -- the signal that something is
    shadowing, reported by ``resolve()`` and ``huginn doctor`` rather than left
    for a reviewer to notice.
    """
    found: list[Any] = []
    seen: list[str] = []          # normalised name of every dist-info walked
    contributors: set[str] = set()  # those that declare a policy
    for distribution in metadata.distributions():
        try:
            raw = distribution.metadata["Name"] or ""
            name = _normalize(raw) if raw else "(unnamed)"
            seen.append(name)
            # A per-dist failure must not abort the walk: one unreadable or
            # corrupt dist-info that stopped discovery would silently widen the
            # permitted set, which is the same C1 failure by another route.
            group = [point for point in distribution.entry_points
                     if point.group == POLICY_ENTRY_POINT_GROUP]
            if not group and _declares_policy_unparseably(distribution):
                # The file names our group but importlib could not parse it, so
                # it yielded nothing -- indistinguishable from "no policy" to
                # every caller. Synthesise a refusing entry point rather than
                # let a corrupt declaration read as permissive (issue #41 C1).
                found.append(_CorruptDeclaration(name))
                contributors.add(name)
                continue
        except Exception:
            LOG.warning("could not read entry points for an installed distribution; "
                        "a model policy it declares would be invisible")
            continue
        if not group:
            continue
        found.extend(group)
        contributors.add(name)
    # A shadowing dist is by construction the one *without* entry_points.txt,
    # so the duplicate has to be spotted among all names walked, not only among
    # those that contributed -- otherwise the masking dist is invisible here too.
    duplicates = tuple(sorted(
        name for name in contributors if seen.count(name) > 1))
    return found, duplicates


def shadowed_policy_distributions() -> tuple[str, ...]:
    """Normalised names of distributions contributing a policy more than once.

    Non-empty means two dist-info directories claim the same distribution name
    and at least one declares a policy -- the shape of the C1 shadowing attack.
    Surfaced by ``huginn doctor`` because doctor's output is the evidence that
    an exclusion is in force.
    """
    return _distribution_entry_points()[1]


def _policy_entry_points() -> list[Any]:
    """Every policy entry point either discovery mechanism can see.

    A *union* on purpose. Policies intersect (see the module docstring), so an
    extra discovery source can only ever add a policy, and adding a policy can
    only narrow what is permitted. Missing one, by contrast, widens it -- which
    is the entire C1 bug. Deduplicated by (name, value) so a policy both
    mechanisms see is not counted twice.
    """
    found, duplicates = _distribution_entry_points()
    if duplicates:
        LOG.error("model policy distributions are shadowed: %s appear more than once on "
                  "sys.path; the entry-point group query cannot see all of them",
                  ", ".join(duplicates))
    try:
        found.extend(entry_points(group=POLICY_ENTRY_POINT_GROUP))
    except Exception:
        # Even total failure of the group query must not widen anything -- the
        # distributions() walk above stands on its own.
        LOG.warning("entry_points(group=%s) failed; relying on the distribution walk",
                    POLICY_ENTRY_POINT_GROUP)
    unique: dict[Any, Any] = {}
    for point in found:
        unique.setdefault((getattr(point, "name", None), getattr(point, "value", None)), point)
    return sorted(unique.values(), key=lambda item: str(getattr(item, "name", "")))


def resolve() -> tuple[ModelPolicy, ...]:
    """Every policy declared by an installed distribution, or the default.

    Deliberately uncached, unlike ``plugins.get_registry()``: resolution is
    cheap (no I/O beyond entry-point metadata the interpreter already read),
    and a memoized permission set is exactly the shape issue #41 calls out as
    unable to express a restriction.
    """
    policies: list[ModelPolicy] = []
    for entry_point in _policy_entry_points():
        try:
            candidate = entry_point.load()
        # BaseException, not Exception -- issue #41 M2: a policy module raising
        # SystemExit at import (a stray `sys.exit()` in a config check, say)
        # otherwise propagates out of every policy function and out of the
        # caller, taking down whatever asked. A policy that cannot load must
        # refuse, never escape.
        except BaseException as exc:
            LOG.error("model policy %s failed to load (%s); refusing every call",
                      entry_point.name, type(exc).__name__)
            policies.append(_load_failed(entry_point.name, exc))
            continue
        if not isinstance(candidate, ModelPolicy):
            LOG.error("model policy %s is not a ModelPolicy; refusing every call",
                      entry_point.name)
            policies.append(_load_failed(
                entry_point.name, TypeError(f"not a ModelPolicy: {type(candidate).__name__}")))
            continue
        policies.append(candidate)
    if not policies:
        return (DEFAULT_POLICY,)
    return tuple(policies)


def refusal(model: str, provider: str) -> str | None:
    """The refusal message for one call, or None when every policy permits it.

    Exposed alongside ``check()`` so read-only surfaces (``GET /api/providers``,
    ``huginn doctor``, settings validation) can report the same verdict without
    catching an exception -- and so a refusal reaches the user as the policy's
    own ``reason``, verbatim.

    An empty ``model`` means "whatever the provider defaults to", which a
    restrictive policy cannot verify, so it is checked like any other id and
    refused unless a pattern matches it. Under a restricted contract a model
    must be named explicitly; the permissive default's ``.*`` matches the
    empty string, so unrestricted builds are unaffected.
    """
    refusing = [policy for policy in resolve() if not _permits(policy, model, provider)]
    if not refusing:
        return None
    reasons = "; ".join(policy.reason or policy.name for policy in refusing)
    return f"refused {provider}:{model or '(provider default)'} -- {reasons}"


def provider_refusal(provider: str) -> str | None:
    """Refusal message when a policy rules out this provider for *any* model.

    Distinct from ``refusal()`` because some surfaces choose a provider before
    a model exists -- the dashboard's provider selector, ``PUT /api/settings``
    validating ``llm.provider``. Only two things can refuse a provider
    outright: a ``require_provider`` naming someone else, and an empty
    ``allow`` (which permits nothing at all, including a load-failure policy).
    A provider this returns None for may still have individual models refused
    later at ``check()``; that is the fail-closed direction.
    """
    refusing = [
        policy for policy in resolve()
        if not policy.allow
        or (policy.require_provider is not None and policy.require_provider != provider)
    ]
    if not refusing:
        return None
    reasons = "; ".join(policy.reason or policy.name for policy in refusing)
    return f"refused provider {provider} -- {reasons}"


def check(model: str, provider: str) -> None:
    """Raise ``PolicyRefused`` unless every resolved policy permits the call.

    This is the entire chokepoint. A core code path that calls a provider
    without coming through here first is a defect -- there is deliberately no
    other supported way to ask "is this call allowed".
    """
    message = refusal(model, provider)
    if message:
        raise PolicyRefused(message)


__all__ = [
    "DEFAULT_POLICY",
    "POLICY_ENTRY_POINT_GROUP",
    "ModelPolicy",
    "PolicyRefused",
    "check",
    "provider_refusal",
    "refusal",
    "resolve",
    "shadowed_policy_distributions",
]
