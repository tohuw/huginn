"""Sanitising untrusted text on its way into a desktop menu -- issue #42.

Both raven projects publish menu rows built out of session names, titles,
transcript-derived topics, `cwd` basenames, and LLM output. Every one of those is
attacker-influenceable text heading for a menu the user reads and clicks, so
nothing becomes a label without passing through :func:`sanitize_label`.

The shared menu host sanitises again at its end. That is not a reason to skip
this, and a reader who thinks it is has the threat model backwards: the host
defends *itself* from a hostile raven, whereas this defends the raven's own users
from hostile content the raven is the one that read. Both are needed, and the
raven's is the side that knows which strings are untrusted.

Extracted because the two implementations were byte-identical regex sets that
each took a while to get right, and the interesting cases (C1 as an alternate CSI
introducer, bidi overrides that reorder a rendered row) are exactly the ones a
second reimplementation misses.

Deliberately *not* included: redaction. Whether a label should have credential
shapes stripped out of it is a per-project decision -- Huginn redacts because a
label can carry a user-set title and plugin summary text that never went through
its transcript-distillation seam, while Muninn does not. A consumer that wants
both composes them (see ``huginn/raven.py``'s ``safe_text``); baking redaction in
here would take that choice away from the project that declined it.
"""
from __future__ import annotations

import re

# CSI/OSC sequences and the short two-character escapes, matched *before* the
# control-character strip below. Order matters: stripping controls first would
# leave the printable tail of "\x1b[31m" behind as the literal text "[31m".
_ANSI_RE = re.compile(
    r"\x1b(?:\[[0-9;:<=>?]*[ -/]*[@-~]"   # CSI ... final byte
    r"|\][^\x07\x1b]*(?:\x07|\x1b\\)"     # OSC ... BEL or ST
    r"|[@-Z\\-_])"                        # two-character escapes
)

# C0 minus the whitespace handled separately, DEL, and C1. C1 is included
# because a lone 0x9b is an alternate CSI introducer on some terminals, so
# stripping ESC alone is not enough.
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")

# Explicit bidi controls (LRE/RLE/PDF/LRO/RLO and the isolates) plus the
# invisible formatting characters used to disguise text. A label without these
# renders as the bytes that produced it; with them, "Quit" can render as "tiuQ"
# and a menu row can read as something it is not.
_SPOOF_RE = re.compile(
    "["
    "​-‏"      # zero-width space/joiners, LRM/RLM
    "‪-‮"      # LRE, RLE, PDF, LRO, RLO
    "⁠-⁤"      # word joiner and invisible operators
    "⁦-⁩"      # LRI, RLI, FSI, PDI
    "﻿"             # BOM / zero-width no-break space
    "]"
)

_WHITESPACE_RE = re.compile(
    r"[\s   -     　]+")

_ELLIPSIS = "…"

#: The shared menu host's own caps. A raven truncates to the same numbers rather
#: than sending longer strings and letting the host cut them: a label trimmed
#: host-side loses its end mid-word with no ellipsis, and the raven is the side
#: that knows where a sensible break is.
#:
#: These integers track the *host protocol*, not corvidae. The names and their
#: meaning are promised; a protocol bump may change the numbers within a CalVer
#: year, so read them rather than copying the values.
MAX_LABEL = 120
MAX_DETAIL = 80


def sanitize_label(value: object, limit: int = MAX_LABEL) -> str:
    """Reduce ``value`` to one bounded, printable, control-free line, or ``""``.

    Non-strings become ``""`` rather than being coerced. Naming the mistake a
    reader might make here: ``str(value)`` looks harmless and is not -- a title
    that arrived as a dict would put ``repr()``'s attacker-chosen punctuation and
    quoting on screen, which is exactly what a spoofed menu row is built out of.

    A string that sanitises to nothing (all escapes, say) returns ``""``. Callers
    treat that as "no label", which either drops the row or substitutes something
    they can vouch for -- the host drops an unlabelled item too, so a row that
    cannot be described must not be rendered as a clickable blank.

    ``limit <= 0`` means "no length cap", which is what a caller wants when it
    intends to transform the text further before capping it. Cap last: clipping
    before a redaction pass could cut a credential shape in half so the pattern
    no longer matches, leaving a partial secret on screen.
    """
    if not isinstance(value, str):
        return ""
    cleaned = _ANSI_RE.sub("", value)
    cleaned = _CONTROL_RE.sub("", cleaned)
    cleaned = _SPOOF_RE.sub("", cleaned)
    # Any ESC left over was not part of a recognised sequence.
    cleaned = cleaned.replace("\x1b", "")
    cleaned = _WHITESPACE_RE.sub(" ", cleaned).strip()
    if limit > 0 and len(cleaned) > limit:
        cleaned = cleaned[: max(limit - 1, 0)].rstrip() + _ELLIPSIS
    return cleaned


__all__ = ["MAX_DETAIL", "MAX_LABEL", "sanitize_label"]
