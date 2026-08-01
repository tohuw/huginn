"""Installed plugin discovery, isolation, and source/provider contracts."""
from __future__ import annotations

import unittest
from unittest.mock import patch

from huginn.bus import Bus
from huginn.config import Config, validate_setting
from huginn.diagnostics import Diagnostics
from huginn.llm.providers import all_providers, blurb_model, compatible_model
from huginn.llm.context import evidence_for_session
from huginn.model import Session
from huginn.plugins import (
    API_VERSION,
    MIN_API_VERSION,
    PluginRegistry,
    PluginSpec,
    SourceContext,
    discover_plugins,
)
from huginn.state import Reducer


class _EntryPoint:
    def __init__(self, name, value=None, error=None):
        self.name = name
        self.value = value
        self.error = error

    def load(self):
        if self.error:
            raise self.error
        return self.value


class _Provider:
    def __init__(self, name="bedrock"):
        self.name = name
        self.label = "Claude on Bedrock"

    def available(self):
        return None

    def compatible_model(self, model):
        return model if model.startswith("anthropic.") else ""

    def resolve_blurb_model(self, model):
        return "anthropic.claude-haiku" if "haiku" in model else self.compatible_model(model)


class _Source:
    name = "workers"

    async def run(self, context):
        context.ok()


class PluginDiscoveryTests(unittest.TestCase):
    def test_valid_plugin_contributes_provider_and_source(self):
        plugin = PluginSpec(
            name="cisco-ai",
            version="1.2.3",
            providers=(_Provider(),),
            sources=(_Source(),),
        )

        registry = discover_plugins([_EntryPoint("cisco-ai", plugin)])

        self.assertEqual([item.name for item in registry.plugins], ["cisco-ai"])
        self.assertEqual(list(registry.providers()), ["bedrock"])
        self.assertEqual(registry.sources()[0][1].name, "workers")
        self.assertEqual(registry.errors, ())

    def test_broken_plugin_does_not_hide_healthy_plugin(self):
        healthy = PluginSpec(name="healthy", version="1", providers=(_Provider(),))

        registry = discover_plugins([
            _EntryPoint("broken", error=RuntimeError("cannot import dependency")),
            _EntryPoint("healthy", healthy),
        ])

        self.assertEqual([item.name for item in registry.plugins], ["healthy"])
        self.assertEqual(registry.errors[0].entry_point, "broken")
        self.assertEqual(registry.errors[0].error_class, "RuntimeError")
        self.assertNotIn("dependency", registry.errors[0].detail)

    def test_incompatible_api_version_is_rejected(self):
        plugin = PluginSpec(name="future", version="1", api_version=API_VERSION + 1)

        registry = discover_plugins([_EntryPoint("future", plugin)])

        self.assertEqual(registry.plugins, ())
        self.assertIn("incompatible", registry.errors[0].detail)


class PluginApiRangeTests(unittest.TestCase):
    """issue #38: an exact `api_version != API_VERSION` comparison meant a
    routine core bump silently disabled every installed plugin. A plugin may
    now declare a supported range, and a mismatch is reported loudly."""

    def test_api_version_only_spec_is_unchanged_when_versions_match(self):
        # The backward-compatibility guarantee: a spec that sets nothing but
        # api_version must behave exactly as it did before ranges existed.
        plugin = PluginSpec(name="legacy", version="1", api_version=API_VERSION,
                            providers=(_Provider(),))

        registry = discover_plugins([_EntryPoint("legacy", plugin)])

        self.assertEqual([item.name for item in registry.plugins], ["legacy"])
        self.assertEqual(registry.errors, ())
        self.assertEqual(plugin.api_range, (API_VERSION, API_VERSION))

    def test_range_spanning_a_core_bump_is_accepted(self):
        # The whole point: a plugin declaring it speaks the current API *and*
        # the next one keeps loading after core bumps API_VERSION.
        plugin = PluginSpec(name="ranged", version="1", api_version=API_VERSION,
                            min_api=MIN_API_VERSION, max_api=API_VERSION + 5)

        registry = discover_plugins([_EntryPoint("ranged", plugin)])

        self.assertEqual([item.name for item in registry.plugins], ["ranged"])
        self.assertEqual(registry.errors, ())

    def test_future_only_range_does_not_overlap_and_is_rejected(self):
        plugin = PluginSpec(name="future", version="1",
                            min_api=API_VERSION + 1, max_api=API_VERSION + 2)

        registry = discover_plugins([_EntryPoint("future", plugin)])

        self.assertEqual(registry.plugins, ())
        self.assertTrue(registry.errors[0].api_mismatch)
        self.assertIn(f"{MIN_API_VERSION}..{API_VERSION}", registry.errors[0].detail)

    def test_past_only_range_does_not_overlap_and_is_rejected(self):
        plugin = PluginSpec(name="ancient", version="1",
                            min_api=MIN_API_VERSION - 3, max_api=MIN_API_VERSION - 1)

        registry = discover_plugins([_EntryPoint("ancient", plugin)])

        self.assertEqual(registry.plugins, ())
        self.assertTrue(registry.errors[0].api_mismatch)

    def test_inverted_range_is_rejected_rather_than_silently_accepted(self):
        plugin = PluginSpec(name="inverted", version="1",
                            min_api=API_VERSION + 1, max_api=API_VERSION - 1)

        registry = discover_plugins([_EntryPoint("inverted", plugin)])

        self.assertEqual(registry.plugins, ())
        self.assertIn("inverted", registry.errors[0].detail)

    def test_mismatch_is_logged_loudly_naming_both_ranges(self):
        # Not a quiet skip: the failure mode issue #38 describes is a plugin
        # that stays installed and stops existing with nothing in the log.
        plugin = PluginSpec(name="future", version="1", api_version=API_VERSION + 1)

        with self.assertLogs("huginn.plugins", level="WARNING") as captured:
            discover_plugins([_EntryPoint("future", plugin)])

        message = "\n".join(captured.output)
        self.assertIn("future", message)
        self.assertIn(f"API {MIN_API_VERSION}..{API_VERSION}", message)
        self.assertIn("still installed", message)

    def test_mismatch_is_flagged_separately_from_an_import_failure(self):
        registry = discover_plugins([
            _EntryPoint("broken", error=ImportError("no module")),
            _EntryPoint("stale", PluginSpec(name="stale", version="1",
                                            api_version=API_VERSION + 1)),
        ])

        by_name = {error.entry_point: error for error in registry.errors}
        self.assertTrue(by_name["stale"].api_mismatch)
        self.assertFalse(by_name["broken"].api_mismatch)
        self.assertEqual([error.entry_point for error in registry.api_mismatches()], ["stale"])

    def test_registry_dict_publishes_core_range_and_plugin_range(self):
        registry = discover_plugins([_EntryPoint("ranged", PluginSpec(
            name="ranged", version="1", min_api=MIN_API_VERSION, max_api=API_VERSION + 5))])

        payload = registry.to_dict()

        self.assertEqual(payload["api_version"], API_VERSION)
        self.assertEqual(payload["min_api_version"], MIN_API_VERSION)
        self.assertEqual(payload["plugins"][0]["api_range"],
                         [MIN_API_VERSION, API_VERSION + 5])

    def test_a_non_int_api_version_is_labelled_an_api_mismatch(self):
        # issue #41 M1: this raised a bare TypeError, so doctor and
        # GET /api/plugins reported api_mismatch False and labelled a version
        # disagreement as an indistinguishable load failure -- the exact
        # mislabelling issue #38 exists to prevent.
        for value in ("1", 1.5, None, [1]):
            with self.subTest(api_version=value):
                registry = discover_plugins([_EntryPoint("odd", PluginSpec(
                    name="odd", version="1", api_version=value))])
                self.assertEqual(registry.plugins, ())
                self.assertTrue(registry.errors[0].api_mismatch)
                self.assertEqual(registry.errors[0].error_class, "PluginApiMismatch")

    def test_a_negative_api_bound_is_rejected(self):
        # min_api=-999 loaded before the fix: the overlap arithmetic is correct,
        # but nothing checked its inputs.
        for kwargs in ({"min_api": -999}, {"max_api": -1}, {"api_version": -5}):
            with self.subTest(**kwargs):
                registry = discover_plugins([_EntryPoint("negative", PluginSpec(
                    name="negative", version="1", **kwargs))])
                self.assertEqual(registry.plugins, ())
                self.assertTrue(registry.errors[0].api_mismatch)
                self.assertIn("negative", registry.errors[0].detail)

    def test_a_bool_api_bound_is_rejected_despite_being_an_int(self):
        # bool is an int subclass, so min_api=True silently meant 1 -- valid by
        # accident, and never what anyone typed on purpose.
        registry = discover_plugins([_EntryPoint("boolean", PluginSpec(
            name="boolean", version="1", min_api=True))])

        self.assertEqual(registry.plugins, ())
        self.assertTrue(registry.errors[0].api_mismatch)
        self.assertIn("bool", registry.errors[0].detail)

    def test_an_unbounded_max_api_cannot_claim_every_future_contract(self):
        # max_api=10**9 asserts forward compatibility with every API that will
        # ever exist, so raising MIN_API_VERSION could never disable the plugin
        # -- which is what raising MIN_API_VERSION is for.
        registry = discover_plugins([_EntryPoint("forever", PluginSpec(
            name="forever", version="1", max_api=10 ** 9))])

        self.assertEqual(registry.plugins, ())
        self.assertTrue(registry.errors[0].api_mismatch)

    def test_declaring_the_next_few_versions_still_works(self):
        # The guarantee issue #38 added must survive M1's validation: a
        # plausible forward range is exactly the supported case.
        registry = discover_plugins([_EntryPoint("ranged", PluginSpec(
            name="ranged", version="1",
            min_api=MIN_API_VERSION, max_api=API_VERSION + 5))])

        self.assertEqual([item.name for item in registry.plugins], ["ranged"])
        self.assertEqual(registry.errors, ())
        # api_range still publishes what the plugin declared, unclamped.
        self.assertEqual(registry.plugins[0].api_range, (MIN_API_VERSION, API_VERSION + 5))

    def test_the_overlap_arithmetic_is_unchanged_at_the_boundaries(self):
        # M1 changed input validation only; pin that the boundaries still land
        # where they did.
        for bounds, loads in (((MIN_API_VERSION, API_VERSION), True),
                              ((API_VERSION, API_VERSION), True),
                              ((API_VERSION + 1, API_VERSION + 1), False),
                              ((MIN_API_VERSION, MIN_API_VERSION), True)):
            low, high = bounds
            with self.subTest(bounds=bounds):
                registry = discover_plugins([_EntryPoint("edge", PluginSpec(
                    name="edge", version="1", min_api=low, max_api=high))])
                self.assertEqual(bool(registry.plugins), loads)

    def test_builtin_provider_name_cannot_be_shadowed(self):
        plugin = PluginSpec(name="shadow", version="1", providers=(_Provider("codex"),))

        registry = discover_plugins([_EntryPoint("shadow", plugin)])

        self.assertEqual(registry.plugins, ())
        self.assertIn("reserved", registry.errors[0].detail)

    def test_duplicate_provider_across_plugins_rejects_second_plugin(self):
        first = PluginSpec(name="first", version="1", providers=(_Provider(),))
        second = PluginSpec(name="second", version="1", providers=(_Provider(),))

        registry = discover_plugins([
            _EntryPoint("first", first),
            _EntryPoint("second", second),
        ])

        self.assertEqual([item.name for item in registry.plugins], ["first"])
        self.assertIn("duplicate provider", registry.errors[0].detail)


class PluginProviderTests(unittest.TestCase):
    def setUp(self):
        self.registry = PluginRegistry((PluginSpec(
            name="bedrock-plugin",
            version="1",
            providers=(_Provider(),),
        ),))

    def test_provider_registry_includes_installed_plugin(self):
        providers = all_providers(self.registry)

        self.assertIn("claude", providers)
        self.assertIsInstance(providers["bedrock"], _Provider)

    def test_plugin_controls_compatible_model_filtering(self):
        self.assertEqual(
            compatible_model("bedrock", "anthropic.claude-v1", self.registry),
            "anthropic.claude-v1",
        )
        self.assertEqual(compatible_model("bedrock", "claude-cli-name", self.registry), "")

    def test_plugin_translates_automatic_model_for_its_backend(self):
        self.assertEqual(
            blurb_model("bedrock", "claude-haiku-4-5", self.registry),
            "anthropic.claude-haiku",
        )

    def test_config_accepts_only_installed_plugin_provider(self):
        with patch("huginn.plugins.get_registry", return_value=self.registry):
            self.assertIsNone(validate_setting("llm", "provider", "bedrock"))
        self.assertIsNotNone(validate_setting("llm", "provider", "not-installed"))


class PluginSourceTests(unittest.TestCase):
    def setUp(self):
        self.bus = Bus()
        self.context = SourceContext(
            plugin_name="neo-cortex",
            source_name="workers",
            config=Config({}),
            bus=self.bus,
            diagnostics=Diagnostics(),
        )

    def test_context_namespaces_session_upsert(self):
        key = self.context.key("node-1:run-2")
        session = Session(
            key=key,
            source="workers",
            session_id="run-2",
            cwd="worker-1/repo",
            name="worker-1-run-2",
            source_summary="status: waiting\nmessage: needs review",
        )

        self.context.upsert(session)
        event = self.bus.events.get_nowait()

        self.assertEqual(event.kind, "plugin.session")
        self.assertEqual(event.session_key, key)
        self.assertIs(event.payload["session"], session)

    def test_context_bounds_authoritative_source_summary(self):
        session = Session(
            key=self.context.key("run-2"),
            source="workers",
            session_id="run-2",
            cwd="worker-1/repo",
            name="worker-1-run-2",
            source_summary="x" * 4001,
        )

        with self.assertRaisesRegex(ValueError, "limited to 4000"):
            self.context.upsert(session)

    def test_context_accepts_a_valid_group_and_label(self):
        session = Session(
            key=self.context.key("run-2"),
            source="workers",
            session_id="run-2",
            cwd="worker-1/repo",
            name="worker-1-run-2",
            group="neo-cortex",
            group_label="Neo-Cortex workers",
        )

        self.context.upsert(session)   # must not raise

    def test_context_rejects_malformed_group_key(self):
        session = Session(
            key=self.context.key("run-2"),
            source="workers",
            session_id="run-2",
            cwd="worker-1/repo",
            name="worker-1-run-2",
            group="Not Valid!",
        )

        with self.assertRaisesRegex(ValueError, "session group must match"):
            self.context.upsert(session)

    def test_context_rejects_group_label_without_group(self):
        session = Session(
            key=self.context.key("run-2"),
            source="workers",
            session_id="run-2",
            cwd="worker-1/repo",
            name="worker-1-run-2",
            group_label="Neo-Cortex workers",
        )

        with self.assertRaisesRegex(ValueError, "group_label requires group"):
            self.context.upsert(session)

    def test_context_rejects_oversized_group_label(self):
        session = Session(
            key=self.context.key("run-2"),
            source="workers",
            session_id="run-2",
            cwd="worker-1/repo",
            name="worker-1-run-2",
            group="neo-cortex",
            group_label="x" * 61,
        )

        with self.assertRaisesRegex(ValueError, "limited to 60"):
            self.context.upsert(session)

    def test_context_accepts_a_valid_source_label(self):
        session = Session(
            key=self.context.key("run-2"),
            source="workers",
            session_id="run-2",
            cwd="worker-1/repo",
            name="worker-1-run-2",
            source_label="NeoCortex",
        )

        self.context.upsert(session)   # must not raise

    def test_context_rejects_blank_source_label(self):
        session = Session(
            key=self.context.key("run-2"),
            source="workers",
            session_id="run-2",
            cwd="worker-1/repo",
            name="worker-1-run-2",
            source_label="   ",
        )

        with self.assertRaisesRegex(ValueError, "source_label must be non-empty"):
            self.context.upsert(session)

    def test_context_rejects_oversized_source_label(self):
        session = Session(
            key=self.context.key("run-2"),
            source="workers",
            session_id="run-2",
            cwd="worker-1/repo",
            name="worker-1-run-2",
            source_label="x" * 41,
        )

        with self.assertRaisesRegex(ValueError, "limited to 40"):
            self.context.upsert(session)

    def test_context_rejects_foreign_session_key(self):
        session = Session(
            key="codex:existing",
            source="codex",
            session_id="existing",
            cwd="/tmp",
            name="existing",
        )

        with self.assertRaisesRegex(ValueError, "must start"):
            self.context.upsert(session)

    def test_context_rejects_mismatched_source_name(self):
        session = Session(
            key=self.context.key("run-2"),
            source="neo-cortex",
            session_id="run-2",
            cwd="worker-1/repo",
            name="worker-1-run-2",
        )

        with self.assertRaisesRegex(ValueError, "source must be workers"):
            self.context.upsert(session)

    def test_source_summary_is_used_as_bounded_session_evidence(self):
        session = Session(
            key=self.context.key("run-2"),
            source="workers",
            session_id="run-2",
            cwd="worker-1/repo",
            name="worker-1-run-2",
            source_summary="status: waiting\nmessage: needs review",
        )

        self.assertEqual(
            evidence_for_session(session),
            ["status: waiting", "message: needs review"],
        )

    def test_reducer_applies_plugin_upsert_and_remove(self):
        key = self.context.key("run-1")
        session = Session(
            key=key,
            source="workers",
            session_id="run-1",
            cwd="node/repo",
            name="run-1",
        )
        reducer = Reducer(Config({}))

        self.context.upsert(session)
        reducer.apply(self.bus.events.get_nowait())
        self.context.remove(key)
        reducer.apply(self.bus.events.get_nowait())

        self.assertNotIn(key, reducer.sessions)
        self.assertEqual(reducer.removed, [key])

    def test_reducer_refreshes_group_on_repeated_upsert(self):
        key = self.context.key("run-1")
        reducer = Reducer(Config({}))
        self.context.upsert(Session(
            key=key, source="workers", session_id="run-1", cwd="node/repo",
            name="run-1", group="neo-cortex", group_label="Neo-Cortex workers",
        ))
        reducer.apply(self.bus.events.get_nowait())
        self.assertEqual(reducer.sessions[key].group, "neo-cortex")
        self.assertEqual(reducer.sessions[key].group_label, "Neo-Cortex workers")

        self.context.upsert(Session(
            key=key, source="workers", session_id="run-1", cwd="node/repo",
            name="run-1", group="neo-cortex", group_label="Neo-Cortex workers",
        ))
        changed = reducer.apply(self.bus.events.get_nowait())
        self.assertEqual(changed, [])   # unchanged group is not a spurious update

    def test_reducer_picks_up_source_label_on_repeated_upsert(self):
        # A session that already exists in the reducer's in-memory state
        # (e.g. surviving a daemon restart) must still pick up source_label
        # from a later upsert -- the merge loop for an existing key touches
        # a fixed attribute list, and a newly added Session field silently
        # never applies to already-known sessions unless it's in that list.
        key = self.context.key("run-1")
        reducer = Reducer(Config({}))
        self.context.upsert(Session(
            key=key, source="workers", session_id="run-1", cwd="node/repo", name="run-1",
        ))
        reducer.apply(self.bus.events.get_nowait())
        self.assertIsNone(reducer.sessions[key].source_label)

        self.context.upsert(Session(
            key=key, source="workers", session_id="run-1", cwd="node/repo", name="run-1",
            source_label="Workers",
        ))
        reducer.apply(self.bus.events.get_nowait())
        self.assertEqual(reducer.sessions[key].source_label, "Workers")

    def test_external_id_is_bounded_and_allowlisted(self):
        with self.assertRaisesRegex(ValueError, "safe characters"):
            self.context.key("../escape")

    def test_existing_keys_are_filtered_to_exact_source_namespace(self):
        context = SourceContext(
            plugin_name="neo-cortex",
            source_name="workers",
            config=Config({}),
            bus=self.bus,
            diagnostics=Diagnostics(),
            _existing_keys=lambda: (
                "plugin:neo-cortex.workers:run-1",
                "plugin:neo-cortex.other:run-2",
                "codex:thread-1",
            ),
        )

        self.assertEqual(
            context.existing_keys(),
            ("plugin:neo-cortex.workers:run-1",),
        )


if __name__ == "__main__":
    unittest.main()
