"""Installed plugin discovery, isolation, and source/provider contracts."""
from __future__ import annotations

import unittest
from unittest.mock import patch

from huginn.bus import Bus
from huginn.config import Config, validate_setting
from huginn.diagnostics import Diagnostics
from huginn.llm.providers import all_providers, compatible_model
from huginn.llm.context import evidence_for_session
from huginn.model import Session
from huginn.plugins import (
    API_VERSION,
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
