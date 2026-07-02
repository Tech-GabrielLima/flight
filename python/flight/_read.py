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
class Mutation:
    """One recorded state write from a `with flight.record()` scope."""

    seq: int
    kind: str  # "local" | "item" | "attr"
    name: str
    key: Optional[str]
    #: (value_kind, value_repr, value_type, value_length)
    value: tuple[str, Optional[str], Optional[str], Optional[int]]
    file: str
    qualname: str
    line: int
    frame: int

    @property
    def value_repr(self) -> str:
        kind, rep, _type, length = self.value
        if rep is not None:
            return rep
        return f"{kind}[{length}]" if length is not None else kind


class Recording:
    """The mutation timeline of a scope `.flight` — the queries that make the
    log useful: per-variable history, state-at-a-point, and who-mutated-what."""

    def __init__(self, mutations: list[Mutation]):
        self.mutations = mutations

    def __len__(self) -> int:
        return len(self.mutations)

    def history(self, name: str) -> list[Mutation]:
        """Every write to local variable `name`, in order — how it evolved."""
        return [m for m in self.mutations if m.kind == "local" and m.name == name]

    def who_mutated(self, name: str) -> list[Mutation]:
        """Every item/attr write to the container/object labelled `name`."""
        return [m for m in self.mutations if m.kind in ("item", "attr") and m.name == name]

    def state_at(self, seq: int) -> dict[str, str]:
        """Reconstruct the locals visible at step `seq`: for each name, the last
        value written at or before `seq` (event sourcing — VISION.md §10)."""
        state: dict[str, str] = {}
        for m in self.mutations:
            if m.seq > seq:
                break
            if m.kind == "local":
                state[m.name] = m.value_repr
        return state

    def names(self) -> list[str]:
        """Distinct local variable names that were written."""
        seen = {}
        for m in self.mutations:
            if m.kind == "local":
                seen[m.name] = None
        return list(seen)


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
    mutation_count: int = 0
    nondet_count: int = 0

    @property
    def is_complete(self) -> bool:
        return not self.partial

    @property
    def has_crash(self) -> bool:
        return self.frame_count > 0 or bool(self.exceptions)

    @property
    def has_mutations(self) -> bool:
        return self.mutation_count > 0

    @property
    def has_nondet(self) -> bool:
        return self.nondet_count > 0

    def tape(self) -> "Tape":
        """Load the recorded non-determinism (NONDET block) as a replay Tape."""
        from ._nondet import Tape

        rows = _core.read_nondet(str(self.path))
        return Tape([(r[0], r[1], r[2], r[3]) for r in rows])

    def tape_json(self) -> Optional[str]:
        if not self.has_nondet:
            return None
        return self.tape().to_json()

    def events(self, limit: int = 500) -> list[tuple[str, str, str, int]]:
        """Up to `limit` most-recent ring events as `(kind, file, qualname,
        line)`, chronological — the execution path before the end."""
        return [tuple(e) for e in _core.read_events(str(self.path), limit)]

    def recording(self) -> Recording:
        """Load the MUTATION timeline (Phase-2 scope recording)."""
        rows = _core.read_mutations(str(self.path))
        muts = [
            Mutation(
                seq=r[0],
                kind=r[1],
                name=r[2],
                key=r[3],
                value=tuple(r[4]),
                file=r[5],
                qualname=r[6],
                line=r[7],
                frame=r[8],
            )
            for r in rows
        ]
        return Recording(muts)

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
        mutation_count=d.get("mutation_count", 0),
        nondet_count=d.get("nondet_count", 0),
    )
