"""The model-policy chokepoint contract (issue #41).

Every LLM call core makes routes through ``huginn.policy.check()``. These
tests pin the invariants that make a restricted-model contract meaningful:
policies intersect and never union, no match means refuse rather than fall
back, and a refusal surfaces the policy's own ``reason`` verbatim. The
permissive default (no policy installed) must remain exactly the behaviour
Huginn had before any of this existed.
"""
from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

from huginn.config import Config, validate_setting
from huginn.policy import (
    DEFAULT_POLICY,
    ModelPolicy,
    PolicyRefused,
    check,
    provider_refusal,
    refusal,
    resolve,
)


def _entry_points(*policies: ModelPolicy):
    """Mirror ``importlib.metadata.entry_points(group=...)``: an iterable of
    objects with ``.name`` and ``.load()``. Patching the function beats
    installing a real distribution -- no subprocess, no sys.path surgery."""
    points = []
    for index, policy in enumerate(policies):
        point = Mock()
        point.name = policy.name or f"policy{index}"
        point.load = Mock(return_value=policy)
        points.append(point)
    return points


def _installed(*policies: ModelPolicy):
    return patch("huginn.policy.entry_points", return_value=_entry_points(*policies))


class PolicyIntersectionTests(unittest.TestCase):
    def test_only_the_overlap_of_two_policies_is_permitted(self):
        # Two narrowing contributors: A allows {a,b}, B allows {b,c}. Only b
        # survives. A union would have permitted all three, which is exactly
        # the failure the additive plugin registry has.
        first = ModelPolicy(name="a", allow=("^a$", "^b$"), reason="policy a")
        second = ModelPolicy(name="b", allow=("^b$", "^c$"), reason="policy b")

        with _installed(first, second):
            check("b", "anyprovider")   # must not raise
            with self.assertRaises(PolicyRefused):
                check("a", "anyprovider")
            with self.assertRaises(PolicyRefused):
                check("c", "anyprovider")

    def test_a_second_policy_can_only_narrow_never_widen(self):
        # A contributor shipping allow=(".*",) alongside a restrictive policy
        # must not restore the models the restrictive one removed.
        restrictive = ModelPolicy(name="approved-only", allow=("^approved$",),
                                  reason="only the approved model")
        permissive = ModelPolicy(name="everything", allow=(".*",), reason="anything goes")

        with _installed(restrictive, permissive):
            check("approved", "anyprovider")
            with self.assertRaises(PolicyRefused):
                check("some-other-model", "anyprovider")


class PolicyFailClosedTests(unittest.TestCase):
    def test_model_no_policy_addresses_is_refused_not_defaulted(self):
        only = ModelPolicy(name="only", allow=("^known$",), reason="only known")

        with _installed(only):
            with self.assertRaises(PolicyRefused):
                check("unlisted", "anyprovider")

    def test_refusal_surfaces_every_refusing_reason_verbatim(self):
        first = ModelPolicy(name="a", allow=("^ok$",), reason="REASON_ALPHA_TOKEN")
        second = ModelPolicy(name="b", allow=("^ok$",), reason="REASON_BETA_TOKEN")

        with _installed(first, second):
            with self.assertRaises(PolicyRefused) as caught:
                check("not-ok", "anyprovider")

        message = str(caught.exception)
        self.assertIn("REASON_ALPHA_TOKEN", message)
        self.assertIn("REASON_BETA_TOKEN", message)

    def test_right_model_wrong_provider_is_refused(self):
        locked = ModelPolicy(name="provider-locked", allow=("^claude-",),
                             require_provider="bedrock", reason="bedrock only")

        with _installed(locked):
            check("claude-sonnet-5", "bedrock")
            with self.assertRaises(PolicyRefused):
                check("claude-sonnet-5", "claude")

    def test_anchored_pattern_rejects_a_lookalike_with_leading_junk(self):
        policy = ModelPolicy(name="anthropic-only", allow=(r"^us\.anthropic\.",),
                             reason="only us.anthropic.* ids")

        with _installed(policy):
            check("us.anthropic.claude-sonnet-5", "bedrock")
            with self.assertRaises(PolicyRefused):
                check("evil-us.anthropic.foo", "bedrock")

    def test_a_policy_that_fails_to_load_refuses_instead_of_vanishing(self):
        # The asymmetry with discover_plugins(), which correctly *skips* a
        # broken plugin: dropping a broken restrictive policy would widen the
        # permitted set, the one thing this module exists to prevent.
        broken = Mock()
        broken.name = "broken-policy"
        broken.load = Mock(side_effect=RuntimeError("credentials missing"))

        with patch("huginn.policy.entry_points", return_value=[broken]):
            resolved = resolve()
            self.assertEqual(len(resolved), 1)
            self.assertEqual(resolved[0].allow, ())
            with self.assertRaises(PolicyRefused):
                check("anything", "anyprovider")

    def test_load_failure_reason_names_only_the_exception_class(self):
        broken = Mock()
        broken.name = "broken-policy"
        broken.load = Mock(side_effect=RuntimeError("sk-ant-PLANTEDSECRET"))

        with patch("huginn.policy.entry_points", return_value=[broken]):
            resolved = resolve()

        self.assertIn("RuntimeError", resolved[0].reason)
        self.assertNotIn("PLANTEDSECRET", resolved[0].reason)

    def test_non_policy_object_refuses_rather_than_being_ignored(self):
        wrong = Mock()
        wrong.name = "not-a-policy"
        wrong.load = Mock(return_value={"allow": [".*"]})

        with patch("huginn.policy.entry_points", return_value=[wrong]):
            self.assertEqual(resolve()[0].allow, ())

    def test_refusal_is_not_retryable(self):
        # The blurb circuit breaker latches permanently on retryable=False; a
        # refused model must not be re-attempted once a minute forever.
        self.assertIs(PolicyRefused("x").retryable, False)


class PermissiveDefaultTests(unittest.TestCase):
    def test_no_installed_policy_permits_everything_via_a_real_policy(self):
        # Existing behaviour must be untouched, and the unrestricted path must
        # run the same intersection code a restricted build does.
        with patch("huginn.policy.entry_points", return_value=[]):
            resolved = resolve()
            self.assertEqual(resolved, (DEFAULT_POLICY,))
            self.assertIsInstance(resolved[0], ModelPolicy)
            check("literally-anything", "any-provider")
            self.assertIsNone(refusal("", "claude"))
            self.assertIsNone(provider_refusal("codex"))


class ProviderRefusalTests(unittest.TestCase):
    def test_require_provider_forbids_every_other_provider_outright(self):
        locked = ModelPolicy(name="bedrock-only", allow=(".*",),
                             require_provider="bedrock", reason="bedrock only")

        with _installed(locked):
            self.assertIsNone(provider_refusal("bedrock"))
            self.assertIn("bedrock only", provider_refusal("claude") or "")

    def test_a_policy_allowing_nothing_forbids_every_provider(self):
        dead = ModelPolicy(name="broken", allow=(), reason="policy failed to load")

        with _installed(dead):
            self.assertIsNotNone(provider_refusal("bedrock"))


class ConfigCannotWidenTests(unittest.TestCase):
    """Config, env, and CLI may narrow but never widen -- so a forbidden value
    is rejected at the settings boundary, not written and refused later."""

    def test_settings_reject_a_provider_the_policy_forbids(self):
        locked = ModelPolicy(name="bedrock-only", allow=(".*",),
                             require_provider="bedrock", reason="bedrock only")
        registry = Mock()
        registry.providers = Mock(return_value={})

        with _installed(locked), patch("huginn.plugins.get_registry", return_value=registry):
            self.assertIsNotNone(validate_setting("llm", "provider", "claude"))

    def test_settings_reject_a_model_the_policy_forbids(self):
        locked = ModelPolicy(name="approved-only", allow=("^approved-model$",),
                             reason="only approved-model")

        with _installed(locked), patch("huginn.config.load",
                                       return_value=Config({"llm": {"provider": "claude"}})):
            self.assertIsNotNone(validate_setting("llm", "chat_model", "forbidden-model"))
            self.assertIsNone(validate_setting("llm", "chat_model", "approved-model"))

    def test_settings_are_unaffected_when_no_policy_is_installed(self):
        with patch("huginn.policy.entry_points", return_value=[]):
            self.assertIsNone(validate_setting("llm", "provider", "claude"))
            self.assertIsNone(validate_setting("llm", "chat_model", "anything-at-all"))


if __name__ == "__main__":
    unittest.main()
