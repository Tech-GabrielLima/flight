"""Reading `.flight` files from Python.

Two levels, both over the native reader (`flight._core`):

- :func:`read` → a :class:`Flight` summary (header, blocks, exception headline,
  event/frame/object counts) — cheap, for scripts and `flight inspect`'s header.
- :meth:`Flight.crash` → a :class:`Crash` with the full detail: the exception
  chain, frames with their locals, the object graph, and source texts.

Phase 1.5's TUI viewer will use the richer query API of `flight-reader`
directly; this is the ergonomic surface for scripts and the CLI.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from . import _core


@dataclass
class Frame:
    """One captured stack frame."""

    file: str
    qualname: str
    lineno: int
    first_lineno: int
    #: (local name, object-graph node id)
    locals: list[tuple[str, int]]


@dataclass
class Crash:
    """The full crash detail of a `.flight`."""

    partial: bool
    #: (exc_type, message, relation) most-recent-first.
    exceptions: list[tuple[str, str, str]]
    frames: list[Frame]
    #: node id -> node dict {kind, repr, type_name, length, truncated, items}.
    objects: dict[int, dict[str, Any]]
    #: filename -> source text.
    sources: dict[str, str]

    def node(self, node_id: int) -> Optional[dict[str, Any]]:
        return self.objects.get(node_id)

    def render(self, node_id: int) -> str:
        """A one-line human rendering of a node (value or type summary)."""
        n = self.objects.get(node_id)
        if n is None:
            return "<missing>"
        if n["repr"] is not None:
            return n["repr"]
        length = n.get("length")
        suffix = f"[{length}]" if length is not None else ""
        return f"{n['kind']}{suffix}"

    def aliases(self, node_id: int) -> list[tuple[int, str]]:
        """Frames/locals where `node_id` appears — the aliasing view."""
        out = []
        for i, fr in enumerate(self.frames):
            for name, oid in fr.locals:
                if oid == node_id:
                    out.append((i, name))
        return out


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
    exceptions: list[tuple[str, str, str]] = field(default_factory=list)
    frame_count: int = 0
    object_count: int = 0

    @property
    def is_complete(self) -> bool:
        return not self.partial

    @property
    def has_crash(self) -> bool:
        return self.frame_count > 0 or bool(self.exceptions)

    def crash(self) -> Crash:
        """Load the full crash detail (frames, locals, object graph, source)."""
        d = dict(_core.read_crash(str(self.path)))
        frames = [
            Frame(
                file=f[0],
                qualname=f[1],
                lineno=f[2],
                first_lineno=f[3],
                locals=[(n, i) for n, i in f[4]],
            )
            for f in d.get("frames", [])
        ]
        objects = {int(k): {**dict(v), "items": [(key, vid) for key, vid in dict(v)["items"]]}
                   for k, v in dict(d.get("objects", {})).items()}
        return Crash(
            partial=d.get("partial", True),
            exceptions=[tuple(e) for e in d.get("exceptions", [])],
            frames=frames,
            objects=objects,
            sources=dict(d.get("sources", {})),
        )


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
        exceptions=[tuple(e) for e in d.get("exceptions", [])],
        frame_count=d.get("frame_count", 0),
        object_count=d.get("object_count", 0),
    )
