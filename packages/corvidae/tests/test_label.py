"""Untrusted text on its way into a desktop menu -- issue #42.

Everything a raven puts in a menu row derives from a transcript, a directory name,
an LLM, or a user-set title, so these are the cases a second reimplementation
misses: C1 as an alternate CSI introducer, the printable tail an ANSI sequence
leaves behind if controls are stripped first, and bidi overrides that reorder a
rendered row so it reads as something other than the bytes behind it.
"""
from __future__ import annotations

import unittest

from corvidae import MAX_DETAIL, MAX_LABEL, sanitize_label


class SanitizeLabelTests(unittest.TestCase):
    def test_plain_text_is_unchanged(self):
        self.assertEqual(sanitize_label("Refactor the parser"), "Refactor the parser")

    def test_ansi_sequences_leave_no_printable_tail(self):
        # The ordering bug worth naming: stripping controls first would leave the
        # literal text "[31m" on screen.
        self.assertEqual(sanitize_label("\x1b[31mQuit\x1b[0m"), "Quit")
        self.assertEqual(sanitize_label("\x1b[1;33;40mbold\x1b[m"), "bold")

    def test_osc_sequences_go_whole(self):
        self.assertEqual(sanitize_label("\x1b]0;window title\x07text"), "text")
        self.assertEqual(sanitize_label("\x1b]8;;http://evil\x1b\\link"), "link")

    def test_two_character_escapes_go(self):
        self.assertEqual(sanitize_label("a\x1bMb"), "ab")

    def test_a_lone_escape_byte_never_survives(self):
        # Anything left over was not part of a recognised sequence, and an ESC on
        # the wire is exactly what this function exists to prevent.
        for value in ("\x1b", "a\x1b", "\x1bZZ\x1b"):
            with self.subTest(value=value):
                self.assertNotIn("\x1b", sanitize_label(value))

    def test_c0_controls_and_del_go(self):
        self.assertEqual(sanitize_label("bell\x07and\x00nul\x7f"), "bellandnul")

    def test_c1_goes_because_0x9b_is_an_alternate_csi(self):
        # Stripping ESC alone is not enough: some terminals read a lone 0x9b as a
        # CSI introducer, so "csi\x9b31m" would still be an escape sequence.
        cleaned = sanitize_label("csi\x9b31m")
        self.assertNotIn("\x9b", cleaned)
        self.assertEqual(cleaned, "csi31m")

    def test_bidi_overrides_and_zero_width_characters_go(self):
        # With these, "Quit" can render as "tiuQ" and a row reads as something it
        # is not.
        self.assertEqual(sanitize_label("‮abcdef"), "abcdef")
        self.assertEqual(sanitize_label("zero​width"), "zerowidth")
        self.assertEqual(sanitize_label("﻿bom"), "bom")
        self.assertEqual(sanitize_label("iso⁦late⁩d"), "isolated")

    def test_newlines_and_tabs_collapse_to_one_line(self):
        # A menu row is one line; a newline in a label is a row that spills.
        self.assertEqual(sanitize_label("line one\nline two"), "line one line two")
        self.assertEqual(sanitize_label("two\n\nlines\tand\ttabs"), "two lines and tabs")

    def test_exotic_whitespace_collapses_too(self):
        self.assertEqual(sanitize_label("a  b　c"), "a b c")

    def test_surrounding_whitespace_is_stripped(self):
        self.assertEqual(sanitize_label("  padded  "), "padded")

    def test_non_strings_become_empty_rather_than_coerced(self):
        # str(value) looks harmless and is not: a title that arrived as a dict
        # would put repr()'s attacker-chosen punctuation and quoting on screen.
        for value in (None, 7, {"label": "x"}, ["a"], b"bytes", object()):
            with self.subTest(value=value):
                self.assertEqual(sanitize_label(value), "")

    def test_text_that_sanitises_to_nothing_is_empty(self):
        # Callers treat "" as "no label", which drops or substitutes the row -- a
        # row that cannot be described must not render as a clickable blank.
        self.assertEqual(sanitize_label("\x1b\x00"), "")
        self.assertEqual(sanitize_label("   "), "")

    def test_over_length_text_is_ellipsised_within_the_limit(self):
        cleaned = sanitize_label("n" * 400)
        self.assertEqual(len(cleaned), MAX_LABEL)
        self.assertTrue(cleaned.endswith("…"))

    def test_the_limit_is_a_character_count_including_the_ellipsis(self):
        cleaned = sanitize_label("abcdefghij", 5)
        self.assertEqual(cleaned, "abcd…")
        self.assertEqual(len(cleaned), 5)

    def test_a_break_does_not_leave_trailing_whitespace_before_the_ellipsis(self):
        self.assertEqual(sanitize_label("ab cdefg", 4), "ab…")

    def test_a_zero_or_negative_limit_means_no_cap(self):
        # What a caller wants when it will transform the text further before
        # capping -- redaction, say, which must run before any clipping.
        self.assertEqual(len(sanitize_label("n" * 400, 0)), 400)
        self.assertEqual(len(sanitize_label("n" * 400, -1)), 400)

    def test_text_at_exactly_the_limit_is_untouched(self):
        self.assertEqual(sanitize_label("x" * 10, 10), "x" * 10)

    def test_the_caps_are_the_hosts_numbers(self):
        # Named constants rather than literals at each call site, because they
        # track the host protocol and may move within a CalVer year.
        self.assertEqual((MAX_LABEL, MAX_DETAIL), (120, 80))

    def test_it_is_idempotent(self):
        # Callers sanitise, transform, and sanitise again (see huginn's safe_text).
        # A second pass must not eat anything the first pass produced.
        for value in ("\x1b[31mQuit\x1b[0m", "a\nb", "n" * 400, "‮x"):
            with self.subTest(value=value):
                once = sanitize_label(value)
                self.assertEqual(sanitize_label(once), once)


if __name__ == "__main__":
    unittest.main()
