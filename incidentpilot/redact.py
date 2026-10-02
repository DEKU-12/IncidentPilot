"""PII redaction applied to log text before it leaves the MCP server."""

from __future__ import annotations

import re

# ponytail: regex catches emails and 13-19 digit card numbers; swap in Cloud DLP (Phase 6) for wider PII types.
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_CARD = re.compile(r"\b(?:\d[ -]?){12,18}\d\b")


def redact(text: str) -> str:
    text = _EMAIL.sub("[EMAIL]", text)
    return _CARD.sub("[CARD]", text)
