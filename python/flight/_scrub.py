"""Scrubbing sensitive values (P5 — privacy by design).

A `.flight` captures real variable values, which may include passwords, tokens
and personal data. Before any byte is written, a dict key or attribute name
matching a sensitive pattern has its *value* replaced by the literal
``<redacted>``. This exists from Phase 1, not as a later patch.
"""

from __future__ import annotations

import re
from typing import Iterable

#: Default patterns (case-insensitive substring-ish) for names whose values are
#: redacted. Deliberately broad — false positives cost nothing, leaks do.
DEFAULT_PATTERNS: tuple[str, ...] = (
    "password",
    "passwd",
    "secret",
    "token",
    "api_key",
    "apikey",
    "authorization",
    "auth",
    "credential",
    "private_key",
    "access_key",
    "session",
    "cookie",
    "card_number",
    "cardnumber",
    "cvv",
    "ssn",
)

REDACTED = "<redacted>"


class Scrubber:
    """Decides whether a given name's value must be redacted."""

    def __init__(self, patterns: Iterable[str] = DEFAULT_PATTERNS):
        # One compiled alternation; word-ish boundaries so "author" doesn't
        # trip "auth" but "auth_token" and "userAuth" do.
        parts = [re.escape(p) for p in patterns]
        self._rx = re.compile("|".join(parts), re.IGNORECASE) if parts else None

    def should_redact(self, name: object) -> bool:
        if self._rx is None or not isinstance(name, str):
            return False
        return self._rx.search(name) is not None
