"""Credential redaction shapes.

Moved from huginn's tests/test_context_privacy.py -- issue #42: redact_secrets
now lives in corvidae, so the pattern coverage lives with it. huginn keeps the
tests that prove its distillation/digest layers actually call through to this.
"""
from __future__ import annotations

import unittest

from corvidae.redact import redact_secrets


class RedactSecretsTests(unittest.TestCase):
    def test_common_credentials_are_redacted(self):
        samples = (
            "key " + "AKIA" + "IOSFODNN7EXAMPLE",
            "Authorization: Bearer abc.def-ghi_123",
            "token=do-not-send",
            "github_" + "pat_1234567890abcdefghijklmnop",
            "https://user:password@example.com/path",
            "eyJabcdefghijk.abcdefghijk.abcdefghijk",
        )
        for sample in samples:
            with self.subTest(sample=sample):
                redacted = redact_secrets(sample)
                self.assertIn("[REDACTED]", redacted)
                self.assertNotIn("do-not-send", redacted)

    def test_slack_and_vendor_api_keys_are_redacted(self):
        # These shapes are named in the documented surface (issue #42), so they
        # get explicit coverage rather than being implied by the pattern tuple.
        samples = (
            "xoxb-" + "1234567890" + "-abcdefghij",
            "sk-ant-" + "api03-abcdefghijklmnopqrstuvwxyz",
            "sk-proj-" + "abcdefghijklmnopqrstuvwxyz",
            "xai-" + "abcdefghijklmnopqrstuvwxyz",
        )
        for sample in samples:
            with self.subTest(sample=sample):
                self.assertEqual(redact_secrets(sample), "[REDACTED]")

    def test_private_key_marker_redacts_whole_evidence_item(self):
        value = "before -----BEGIN PRIVATE KEY----- private material"
        self.assertEqual(redact_secrets(value), "[REDACTED PRIVATE KEY]")

    def test_url_credentials_keep_the_scheme_and_drop_the_userinfo(self):
        self.assertEqual(
            redact_secrets("clone https://alice:hunter2@example.com/repo.git"),
            "clone https://[REDACTED]@example.com/repo.git",
        )

    def test_ordinary_text_is_untouched(self):
        for sample in ("feature/plugin-api", "just a normal sentence", ""):
            with self.subTest(sample=sample):
                self.assertEqual(redact_secrets(sample), sample)


if __name__ == "__main__":
    unittest.main()
