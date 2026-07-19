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

    def test_string_list_type_enforced(self):
        self.assertIsNone(validate_setting("patterns", "permission", ["a", "b"]))
        self.assertIsNotNone(validate_setting("patterns", "permission", "not-a-list"))
        self.assertIsNotNone(validate_setting("patterns", "permission", ["a", 1]))

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
        cfg.update("patterns", "waiting", [value])
        config.save(cfg)
        reloaded = config.load()
        return reloaded.section("patterns")["waiting"][0]

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


if __name__ == "__main__":
    unittest.main()
