"""Reading `.flight` files from Python.

Thin wrapper over the native reader (`flight._core.read_summary`), which parses
the file, tolerates truncation, and returns a plain summary dict. Phase 1.5's
TUI viewer will use the richer query API of `flight-reader` directly; this is
the ergonomic surface for scripts and the CLI.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import _core


@dataclass
class Flight:
    """A parsed `.flight` file summary."""

    path: Path
    format_version: int
    flight_version: str
    created_unix_ms: int
    partial: bool
    used_index: bool
    blocks: list[str]
    meta: dict[str, Any]
    event_count: int
    wrapped: bool
    code_count: int
    recent_events: list[tuple[str, str, int]]

    @property
    def is_complete(self) -> bool:
        return not self.partial


def read(path) -> Flight:
    """Parse a `.flight` file and return a :class:`Flight` summary."""
    d = dict(_core.read_summary(str(path)))
    return Flight(
        path=Path(path),
        format_version=d.get("format_version", 0),
        flight_version=d.get("flight_version", ""),
        created_unix_ms=d.get("created_unix_ms", 0),
        partial=d.get("partial", True),
        used_index=d.get("used_index", False),
        blocks=list(d.get("blocks", [])),
        meta=dict(d.get("meta", {})),
        event_count=d.get("event_count", 0),
        wrapped=d.get("wrapped", False),
        code_count=d.get("code_count", 0),
        recent_events=[tuple(e) for e in d.get("recent_events", [])],
    )
