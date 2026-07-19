"""Settings validation and TOML round-tripping -- issue #18."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from huginn import config
from huginn.config import Config, validate_setting


class ValidateSettingTests(unittest.TestCase):
    def test_unknown_section_or_key_rejected(self):
        self.assertIsNotNone(validate_setting("nope", "x", 1))
        self.assertIsNotNone(validate_setting("ui", "nope", 1))

    def test_bool_type_enforced(self):
        self.assertIsNone(validate_setting("ui", "show_ended", False))
        self.assertIsNotNone(validate_setting("ui", "show_ended", "yes"))
        self.assertIsNotNone(validate_setting("ui", "show_ended", 1))   # bool, not int-as-bool

    def test_int_type_enforced_and_bool_rejected(self):
        self.assertIsNone(validate_setting("ui", "ended_ttl_s", 300))
        self.assertIsNotNone(validate_setting("ui", "ended_ttl_s", 3.5))
        self.assertIsNotNone(validate_setting("ui", "ended_ttl_s", True))

    def test_positive_numeric_keys_reject_zero_and_negative(self):
        for value in (0, -1, -300):
            self.assertIsNotNone(validate_setting("ui", "ended_ttl_s", value), msg=value)
            self.assertIsNotNone(validate_setting("codex", "poll_s", float(value)), msg=value)

    def test_float_accepts_int_too(self):
        self.assertIsNone(validate_setting("codex", "poll_s", 5))
        self.assertIsNone(validate_setting("codex", "poll_s", 5.5))

    def test_port_range_enforced(self):
        self.assertIsNone(validate_setting("server", "port", 47100))
        self.assertIsNotNone(validate_setting("server", "port", 0))
        self.assertIsNotNone(validate_setting("server", "port", 70000))

    def test_provider_enum_enforced(self):
        self.assertIsNone(validate_setting("llm", "provider", "codex"))
        self.assertIsNotNone(validate_setting("llm", "provider", "gpt5"))

    def test_chat_span_enum_enforced(self):
        self.assertIsNone(validate_setting("ui", "chat_span", "horizontal"))
        self.assertIsNone(validate_setting("ui", "chat_span", "vertical"))
        self.assertIsNotNone(validate_setting("ui", "chat_span", "diagonal"))

    def test_sort_enum_enforced(self):
        for value in ("state", "alpha", "newest", "oldest"):
            self.assertIsNone(validate_setting("ui", "sort", value))
        self.assertIsNotNone(validate_setting("ui", "sort", "random"))

    def test_string_list_type_enforced(self):
        self.assertIsNone(validate_setting("patterns", "permission", ["a", "b"]))
        self.assertIsNotNone(validate_setting("patterns", "permission", "not-a-list"))
        self.assertIsNotNone(validate_setting("patterns", "permission", ["a", 1]))

    def test_patterns_waiting_removed(self):
        # issue #19: the string-match fallback is a binary permission/
        # not-permission classification -- a separate "waiting" pattern
        # list could never affect behavior, so it was removed rather than
        # left as a config knob that does nothing.
        self.assertNotIn("waiting", config.DEFAULTS["patterns"])
        self.assertIsNotNone(validate_setting("patterns", "waiting", ["x"]))

    def test_plain_string_type_enforced(self):
        self.assertIsNone(validate_setting("llm", "provider", "claude"))
        self.assertIsNotNone(validate_setting("llm", "chat_model", 5))


class TomlRoundTripTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_config_dir = config.CONFIG_DIR
        self.old_config_path = config.CONFIG_PATH
        config.CONFIG_DIR = Path(self.tmp.name)
        config.CONFIG_PATH = config.CONFIG_DIR / "config.toml"

    def tearDown(self):
        config.CONFIG_DIR = self.old_config_dir
        config.CONFIG_PATH = self.old_config_path
        self.tmp.cleanup()

    def _round_trip(self, value: str) -> str:
        cfg = Config({})
        cfg.update("patterns", "permission", [value])
        config.save(cfg)
        reloaded = config.load()
        return reloaded.section("patterns")["permission"][0]

    def test_quotes_and_backslashes(self):
        self.assertEqual(self._round_trip('say "hi" \\ bye'), 'say "hi" \\ bye')

    def test_newline_and_tab(self):
        self.assertEqual(self._round_trip("line1\nline2\ttabbed"), "line1\nline2\ttabbed")

    def test_carriage_return_and_other_control_chars(self):
        self.assertEqual(self._round_trip("a\rb\x00c\x1fd"), "a\rb\x00c\x1fd")

    def test_unicode_preserved(self):
        self.assertEqual(self._round_trip("waiting… 等待 🚀"), "waiting… 等待 🚀")

    def test_empty_string(self):
        self.assertEqual(self._round_trip(""), "")

    def test_save_drops_keys_no_longer_in_defaults(self):
        # issue #19: a config.toml written before "waiting" was removed
        # from DEFAULTS carried it forward on every save indefinitely.
        config.CONFIG_PATH.write_text('[patterns]\npermission = ["x"]\nwaiting = ["y"]\n')
        cfg = config.load()
        self.assertIn("waiting", cfg.section("patterns"))   # still present pre-save
        config.save(cfg)
        reloaded = config.load()
        self.assertNotIn("waiting", reloaded.section("patterns"))
        self.assertEqual(reloaded.section("patterns")["permission"], ["x"])


if __name__ == "__main__":
    unittest.main()
