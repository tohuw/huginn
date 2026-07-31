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
from dataclasses import dataclass
from importlib.metadata import entry_points

POLICY_ENTRY_POINT_GROUP = "huginn.policy"
LOG = logging.getLogger("huginn.policy")


@dataclass(frozen=True)
class ModelPolicy:
    """One installed distribution's statement of which calls it permits.

    ``allow`` patterns are matched with ``re.search``, not ``re.fullmatch``: a
    policy author who wants a strict prefix restriction anchors the pattern
    with ``^`` themselves. Unanchored search is deliberate -- it lets a policy
    allowlist a vendor prefix embedded in a longer qualified id (for example
    ``bedrock/us.anthropic.claude-...``) without every author remembering to
    write ``.*`` first. Anchoring stays a per-pattern choice because a global
    fullmatch would silently break every existing unanchored pattern the
    moment one author wanted a suffix match.

    An empty ``allow`` tuple permits nothing. That is the correct reading, not
    a degenerate one: see ``_load_failed`` below.
    """

    name: str
    allow: tuple[str, ...]                 # regex allowlist of model ids (re.search)
    require_provider: str | None = None    # None = any provider
    reason: str = ""                       # shown verbatim on refusal


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
    if policy.require_provider is not None and provider != policy.require_provider:
        return False
    return any(re.search(pattern, model) for pattern in policy.allow)


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


def resolve() -> tuple[ModelPolicy, ...]:
    """Every policy declared by an installed distribution, or the default.

    Deliberately uncached, unlike ``plugins.get_registry()``: resolution is
    cheap (no I/O beyond entry-point metadata the interpreter already read),
    and a memoized permission set is exactly the shape issue #41 calls out as
    unable to express a restriction.
    """
    policies: list[ModelPolicy] = []
    for entry_point in sorted(entry_points(group=POLICY_ENTRY_POINT_GROUP),
                              key=lambda item: item.name):
        try:
            candidate = entry_point.load()
        except Exception as exc:
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
]
