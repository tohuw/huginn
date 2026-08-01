"""Credential redaction for text on its way out of an agent transcript -- issue #42.

Extracted from ``huginn.llm.context`` unchanged. Only ``redact_secrets`` is
shared: the surrounding distillation in Huginn (``distill_claude``, digests,
clipping) is dashboard-shaped and stayed behind, deliberately. What travels here
is the part every consumer needs and nobody wants to re-derive -- the pattern set
guarding the seam where transcript bytes become prompt text or UI text.

Redaction is best-effort defence in depth, not a guarantee: it recognises common
credential *shapes*, so a novel or unshaped secret can pass through. Treat it as
one layer, never as the reason it is safe to ship transcript text somewhere.
"""
from __future__ import annotations

import re

# A private-key marker means the surrounding text is key material; the whole
# item is dropped rather than pattern-matched line by line, because PEM bodies
# are ordinary base64 and would otherwise survive intact.
_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----",
    re.IGNORECASE,
)
_URL_CREDENTIAL_RE = re.compile(r"(?i)\b(https?://)[^\s/@:]+:[^\s/@]+@")
_SECRET_PATTERNS = (
    re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
    re.compile(r"\b(?:sk-ant-|sk-proj-|xai-)[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+\-/]+=*"),
    re.compile(r"(?i)\b(?:password|passwd|secret|token|api[_-]?key)\s*[:=]\s*[^\s,;]+"),
)


def redact_secrets(text: str) -> str:
    """Redact common credential shapes before evidence leaves the transcript seam."""
    if _PRIVATE_KEY_RE.search(text):
        return "[REDACTED PRIVATE KEY]"
    value = _URL_CREDENTIAL_RE.sub(r"\1[REDACTED]@", text)
    for pattern in _SECRET_PATTERNS:
        value = pattern.sub("[REDACTED]", value)
    return value


__all__ = ["redact_secrets"]
