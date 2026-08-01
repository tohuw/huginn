"""The declared surface, enforced -- issue #42.

README.md makes two structural promises that are easy to break by accident: the
exact set of names re-exported from ``corvidae``, and the one-way dependency
direction (corvidae must never import huginn). A prose promise nobody checks is
what the issue was complaining about, so these check it.
"""
from __future__ import annotations

import ast
import inspect
import unittest
from pathlib import Path

import corvidae

PACKAGE_ROOT = Path(corvidae.__file__).parent

# Changing this list is a compatibility event, not a refactor: removing a name
# breaks a consumer, and adding one silently widens what we owe support for.
# Update it deliberately, with the README, and mind the CalVer year.
STABLE_SURFACE = {
    "ATTACH_WINDOW",
    "ATTENTION_STATES",
    "ClaudeAnalyzer",
    "CodexAnalyzer",
    "MAX_ATTACH_LINE",
    "MAX_READ",
    "STATE_RANK",
    "Session",
    "SessionState",
    "Tail",
    "redact_secrets",
}


class StableSurfaceTests(unittest.TestCase):
    def test_all_matches_the_declared_surface(self):
        self.assertEqual(set(corvidae.__all__), STABLE_SURFACE)

    def test_every_declared_name_is_actually_importable_from_the_root(self):
        for name in sorted(STABLE_SURFACE):
            with self.subTest(name=name):
                self.assertTrue(hasattr(corvidae, name))

    def test_tail_signature_is_unchanged(self):
        self.assertEqual(
            list(inspect.signature(corvidae.Tail.__init__).parameters),
            ["self", "path"],
        )
        for method in ("attach", "read_new", "read_available"):
            with self.subTest(method=method):
                params = inspect.signature(getattr(corvidae.Tail, method)).parameters
                self.assertEqual(list(params), ["self"])

    def test_redact_secrets_signature_is_unchanged(self):
        self.assertEqual(
            list(inspect.signature(corvidae.redact_secrets).parameters), ["text"])

    def test_analyzer_signatures_are_unchanged(self):
        for cls in (corvidae.ClaudeAnalyzer, corvidae.CodexAnalyzer):
            with self.subTest(cls=cls.__name__):
                self.assertEqual(
                    list(inspect.signature(cls.feed).parameters), ["self", "entries"])
                self.assertEqual(
                    list(inspect.signature(cls.activity).parameters), ["self"])

    def test_documented_activity_keys_are_all_present(self):
        self.assertLessEqual(
            {"pending_tools", "oldest_pending_age", "last_entry_type", "last_prompt",
             "last_assistant_text", "asked_user_question", "git_branch", "model",
             "tokens", "error", "last_ts", "subagents"},
            set(corvidae.ClaudeAnalyzer().activity()),
        )
        self.assertLessEqual(
            {"phase", "last_prompt", "last_assistant_text", "model", "tokens", "last_ts"},
            set(corvidae.CodexAnalyzer().activity()),
        )


class DependencyDirectionTests(unittest.TestCase):
    """corvidae must be installable, and useful, with no huginn present."""

    def test_no_module_imports_huginn(self):
        offenders = []
        for path in sorted(PACKAGE_ROOT.rglob("*.py")):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [node.module or ""]
                else:
                    continue
                for name in names:
                    if name == "huginn" or name.startswith("huginn."):
                        offenders.append(f"{path.name}:{node.lineno} -> {name}")

        self.assertEqual(offenders, [])

    def test_declares_no_third_party_dependencies(self):
        # Skipped when running against an installed wheel, where there is no
        # sibling pyproject.toml -- the source-tree run is what gates a change.
        pyproject = PACKAGE_ROOT.parent / "pyproject.toml"
        if not pyproject.is_file():
            self.skipTest("not running from the corvidae source tree")

        self.assertIn("dependencies = []", pyproject.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
