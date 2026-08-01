"""The model-policy chokepoint contract (issue #41).

Every LLM call core makes routes through ``huginn.policy.check()``. These
tests pin the invariants that make a restricted-model contract meaningful:
policies intersect and never union, no match means refuse rather than fall
back, and a refusal surfaces the policy's own ``reason`` verbatim. The
permissive default (no policy installed) must remain exactly the behaviour
Huginn had before any of this existed.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
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
    shadowed_policy_distributions,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


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


class AllowValidationTests(unittest.TestCase):
    """issue #41 H1/H2: ``allow`` was a type hint on a dataclass that validated
    nothing, so one missing comma turned a vendor-prefix allowlist into
    allow-everything, and a malformed regex escaped as ``re.error``."""

    def test_a_backslash_free_bare_string_allow_is_rejected(self):
        # The case that proves the *tuple* check does the work. Split into
        # characters, "^us-anthropic-" has every character compile cleanly, so
        # eager re.compile catches nothing and the lone "^" then matches every
        # model id under re.search. Verified before the fix.
        with self.assertRaises(TypeError):
            ModelPolicy(name="typo", allow="^us-anthropic-")

        # Pin the premise, so this test cannot start passing for the wrong
        # reason if the type check is ever removed in favour of compilation.
        for character in "^us-anthropic-":
            re.compile(character)   # must not raise
        self.assertTrue(re.search("^", "local-llama-7b"))

    def test_a_bare_string_allow_with_backslashes_is_also_rejected(self):
        # The brief's original example. It happens to yield a lone "\\" that
        # fails to compile, so it would be caught either way -- which is exactly
        # why it is the weaker test of the two.
        with self.assertRaises(TypeError):
            ModelPolicy(name="typo", allow=r"^us\.anthropic\.")

    def test_a_non_string_allow_element_is_rejected(self):
        with self.assertRaises(TypeError):
            ModelPolicy(name="typed", allow=(re.compile("^a$"),))  # type: ignore[arg-type]

    def test_a_non_tuple_allow_is_rejected(self):
        with self.assertRaises(TypeError):
            ModelPolicy(name="listy", allow=["^a$"])  # type: ignore[arg-type]

    def test_a_malformed_regex_is_rejected_at_construction(self):
        with self.assertRaises(re.error):
            ModelPolicy(name="broken-regex", allow=("[unclosed",))

    def test_a_policy_failing_validation_becomes_a_refusing_policy(self):
        # The whole point of eager validation: an unusable policy must land on
        # the refuse-everything _load_failed path, never be trusted and never
        # vanish (which would widen the permitted set).
        bad = Mock()
        bad.name = "typo-policy"
        bad.value = "typo:POLICY"
        bad.load = Mock(side_effect=TypeError("allow must be a tuple"))

        with patch("huginn.policy._policy_entry_points", return_value=[bad]):
            resolved = resolve()
            self.assertEqual(resolved[0].allow, ())
            with self.assertRaises(PolicyRefused):
                check("anything", "anyprovider")

    def test_a_matching_fault_refuses_rather_than_raising(self):
        # H2: re.error is not PolicyRefused, so it 500s /api/providers and PUT
        # /api/settings, and in blurb.py lands in a broad handler where
        # retryable defaults to True -- retrying with backoff instead of
        # latching. Anything unexpected during matching must mean refuse.
        policy = ModelPolicy(name="explodes", allow=("^ok$",), reason="only ok")
        exploding = Mock()
        exploding.search = Mock(side_effect=re.error("synthetic match failure"))
        object.__setattr__(policy, "_patterns", (exploding,))

        with patch("huginn.policy._policy_entry_points",
                   return_value=[_point("explodes", policy)]):
            self.assertIsNotNone(refusal("ok", "anyprovider"))
            with self.assertRaises(PolicyRefused):
                check("ok", "anyprovider")

    def test_ctrl_c_during_matching_stays_interruptible(self):
        # The deliberate asymmetry with the *load* guard, which does catch
        # BaseException. A pathological pattern can backtrack for a long time,
        # which is exactly when a user reaches for Ctrl-C -- swallowing that to
        # report a refusal would make the call unkillable. A refusal is not
        # worth an uninterruptible process.
        policy = ModelPolicy(name="slow", allow=("^ok$",), reason="only ok")
        interrupting = Mock()
        interrupting.search = Mock(side_effect=KeyboardInterrupt())
        object.__setattr__(policy, "_patterns", (interrupting,))

        with patch("huginn.policy._policy_entry_points",
                   return_value=[_point("slow", policy)]):
            with self.assertRaises(KeyboardInterrupt):
                refusal("ok", "anyprovider")


def _point(name: str, policy: ModelPolicy):
    point = Mock()
    point.name = name
    point.value = f"{name}:POLICY"
    point.load = Mock(return_value=policy)
    return point


class PolicyImportEscapeTests(unittest.TestCase):
    """issue #41 M2: ``resolve()`` caught ``Exception``, so a policy module
    raising ``SystemExit`` at import propagated out of every policy function
    and out of whatever called it."""

    def test_system_exit_at_policy_import_refuses_instead_of_escaping(self):
        exiting = Mock()
        exiting.name = "exits-at-import"
        exiting.value = "exits:POLICY"
        exiting.load = Mock(side_effect=SystemExit("a stray sys.exit in a config check"))

        with patch("huginn.policy._policy_entry_points", return_value=[exiting]):
            resolved = resolve()   # must not raise SystemExit
            self.assertEqual(resolved[0].allow, ())
            self.assertIn("SystemExit", resolved[0].reason)
            with self.assertRaises(PolicyRefused):
                check("anything", "anyprovider")

    def test_keyboard_interrupt_at_policy_import_also_refuses(self):
        interrupted = Mock()
        interrupted.name = "interrupted"
        interrupted.value = "interrupted:POLICY"
        interrupted.load = Mock(side_effect=KeyboardInterrupt())

        with patch("huginn.policy._policy_entry_points", return_value=[interrupted]):
            self.assertEqual(resolve()[0].allow, ())


class RealDistInfoDiscoveryTests(unittest.TestCase):
    """issue #41 C1, on real ``.dist-info`` directories.

    The rest of this file patches discovery, which by construction cannot catch
    a bug *in* discovery: ``entry_points()`` dedupes distributions by normalised
    name, first on ``sys.path`` wins, so a directory holding only
    ``mypol-9.9.dist-info/METADATA`` -- same name, no ``entry_points.txt`` --
    masked the real ``mypol`` and ``resolve()`` fell back to the permissive
    default. These tests therefore build the dist-info on disk and resolve in a
    subprocess, because ``sys.path`` order is the mechanism under test.
    """

    PROBE = textwrap.dedent("""
        import json, sys
        import huginn.policy as policy
        print(json.dumps({
            "names": [p.name for p in policy.resolve()],
            "refusal": policy.refusal("gpt-4o", "openai"),
            "shadowed": list(policy.shadowed_policy_distributions()),
        }))
    """)

    def _resolve_with_path(self, *path_entries: Path) -> dict:
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join(str(entry) for entry in path_entries)
        result = subprocess.run(
            [sys.executable, "-c", self.PROBE],
            capture_output=True, text=True, cwd=str(REPO_ROOT), env=env, timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout.strip().splitlines()[-1])

    @staticmethod
    def _real_dist(root: Path) -> Path:
        """A distribution named ``mypol`` that declares a restrictive policy."""
        site = root / "real"
        info = site / "mypol-1.0.dist-info"
        info.mkdir(parents=True)
        (info / "METADATA").write_text("Metadata-Version: 2.1\nName: mypol\nVersion: 1.0\n")
        (info / "entry_points.txt").write_text("[huginn.policy]\napproved = mypolmod:POLICY\n")
        (site / "mypolmod.py").write_text(textwrap.dedent(r"""
            from huginn.policy import ModelPolicy
            POLICY = ModelPolicy(
                name="approved", allow=(r"^us\.anthropic\.",),
                require_provider="bedrock", reason="approved provider only")
        """))
        return site

    @staticmethod
    def _shadow_dist(root: Path) -> Path:
        """A metadata-only dist claiming the same normalised name and nothing else."""
        site = root / "shadow"
        info = site / "mypol-9.9.dist-info"
        info.mkdir(parents=True)
        (info / "METADATA").write_text("Metadata-Version: 2.1\nName: mypol\nVersion: 9.9\n")
        return site

    def test_the_real_policy_is_found_and_refuses(self):
        with tempfile.TemporaryDirectory() as tmp:
            found = self._resolve_with_path(self._real_dist(Path(tmp)))
        self.assertEqual(found["names"], ["approved"])
        self.assertIn("approved provider only", found["refusal"] or "")
        self.assertEqual(found["shadowed"], [])

    def test_a_metadata_only_shadow_does_not_suppress_the_policy(self):
        # Before the fix: names == ["default"] and refusal is None -- the
        # excluded model became usable again by planting one METADATA file
        # earlier on sys.path, which a writable checkout is enough for.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            found = self._resolve_with_path(self._shadow_dist(root), self._real_dist(root))
        self.assertEqual(found["names"], ["approved"])
        self.assertIn("approved provider only", found["refusal"] or "")

    def test_shadowing_is_reported_not_merely_survived(self):
        # Surviving it silently would leave no way to tell an exclusion is
        # under attack; doctor's output is the evidence issue #41 relies on.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            found = self._resolve_with_path(self._shadow_dist(root), self._real_dist(root))
        self.assertEqual(found["shadowed"], ["mypol"])

    def test_a_corrupt_declaration_refuses_rather_than_reading_as_unrestricted(self):
        # Same failure class: importlib parses a truncated entry_points.txt to
        # zero entry points *silently*, so an installed restrictive policy is
        # indistinguishable from no policy at all.
        with tempfile.TemporaryDirectory() as tmp:
            site = self._real_dist(Path(tmp))
            (site / "mypol-1.0.dist-info" / "entry_points.txt").write_text(
                "[huginn.policy\napproved = ")
            found = self._resolve_with_path(site)
        self.assertNotEqual(found["names"], ["default"])
        self.assertIsNotNone(found["refusal"])

    def test_nothing_installed_still_permits_everything(self):
        # The default contract: no policy installed means unrestricted, exactly
        # as before issue #41.
        with tempfile.TemporaryDirectory() as tmp:
            found = self._resolve_with_path(Path(tmp))
        self.assertEqual(found["names"], ["default"])
        self.assertIsNone(found["refusal"])

    def test_shadow_reporting_is_quiet_on_this_working_tree(self):
        # A false positive here would make the signal useless in practice.
        self.assertEqual(shadowed_policy_distributions(), ())


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
