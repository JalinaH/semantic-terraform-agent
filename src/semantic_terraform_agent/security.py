"""Small, deterministic redaction helpers for externalized command/model context."""

from __future__ import annotations

import re


SECRET_PATTERNS = (
    re.compile(r"(?i)(api[_-]?key|secret|token|password)(\s*[:=]\s*)([^\s,;]+)"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b"),
)


def redact_secrets(value: str) -> str:
    redacted = value
    for index, pattern in enumerate(SECRET_PATTERNS):
        if index == 0:
            redacted = pattern.sub(r"\1\2[REDACTED]", redacted)
        else:
            redacted = pattern.sub("[REDACTED]", redacted)
    return redacted

