from __future__ import annotations

import re
from typing import Iterable

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

    def __init__(self, patterns: Iterable[str] = DEFAULT_PATTERNS):
        parts = [re.escape(p) for p in patterns]
        self._rx = re.compile("|".join(parts), re.IGNORECASE) if parts else None

    def should_redact(self, name: object) -> bool:
        if self._rx is None or not isinstance(name, str):
            return False
        return self._rx.search(name) is not None
