"""Prompt-injection guard for log text before it reaches the model.

Logs contain text that outsiders control (coupon codes, emails, headers). Text that
addresses the AI agent or tries to give it orders is replaced before the model sees it.
Set INCIDENTPILOT_GUARD=off to measure how the agent does without it.
"""

from __future__ import annotations

import os
import re

# ponytail: regex heuristics for text aimed at the agent; add an LLM classifier if red-team evals show misses.
_PATTERNS = re.compile(
    "|".join(
        [
            r"\bignore (?:all |any )?(?:previous|prior|above|earlier) (?:instructions|rules|guidance)",
            r"\b(?:ai|llm) (?:agent|assistant|model|bot)\b",
            r"\bon-?call (?:ai|agent|bot|assistant)\b",
            r"\b(?:note|message|instructions?) (?:to|for) (?:the )?(?:ai|agent|assistant|model|bot)\b",
            r"\bincidentpilot\b",
            r"(?:^|[\s'\"(])(?:system|assistant)\s*:",
        ]
    ),
    re.IGNORECASE,
)

QUARANTINED = "[QUARANTINED: log text that addressed the AI agent was withheld]"


def enabled() -> bool:
    return os.environ.get("INCIDENTPILOT_GUARD", "on").lower() != "off"


def looks_like_injection(text: str) -> bool:
    return bool(_PATTERNS.search(text))


def screen(text: str) -> tuple[str, bool]:
    """(text the model may see, whether it was quarantined)."""
    if enabled() and looks_like_injection(text):
        return QUARANTINED, True
    return text, False
