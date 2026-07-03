"""Phase 5 — the reverse debugger engine (time-travel over a scope recording).

Phase 2 already records every state write of a `with flight.record()` block as a
MUTATION with its exact line and sequence number, and `Recording.state_at(seq)`
reconstructs the locals at any point (event sourcing). Phase 5 turns that data
into the *experience* of a reverse debugger: a cursor you can step **backward**
and forward through, and a **breakpoint in the past** — "stop at the write where
`running` first passed 100" — answered by searching the timeline.

This module is pure logic over a :class:`~flight._read.Recording`: no terminal,
no protocol. The DAP adapter (`_dap`) and the CLI are thin shells over it, so the
interesting behaviour is unit-tested without an editor or a socket.

Position model (event sourcing): ``pos`` is the number of writes that have
"executed", 0..N. The *current* step is ``steps[pos-1]`` (None at pos 0), and the
state at the cursor is the reconstruction of the first ``pos`` writes. A session
starts at the **end** (``pos == N``) — you are at the final state and walk back,
the post-mortem stance — and every navigation lands the cursor *on* a write.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Callable, Optional


@dataclass(frozen=True)
class Step:
    """One position in the timeline — a single recorded state write."""

    index: int  # 0-based position in the (seq-sorted) timeline
    seq: int
    kind: str  # "local" | "item" | "attr"
    name: str
    key: Optional[str]
    file: str
    qualname: str
    line: int
    frame: int
    value_repr: str
    raw: tuple  # the recorded (kind, repr, type, length) — for coercion

    @property
    def where(self) -> str:
        return f"{os.path.basename(self.file)}:{self.line}"

    @property
    def target(self) -> str:
        if self.kind == "local":
            return self.name
        return f"{self.name}[{self.key}]" if self.kind == "item" else f"{self.name}.{self.key}"

    def describe(self) -> str:
        return f"#{self.seq} {self.where} {self.kind} {self.target} = {self.value_repr}"


# -- value coercion & predicates -------------------------------------------

_OPS: dict[str, Callable[[object, object], bool]] = {
    "==": lambda a, b: a == b,
    "!=": lambda a, b: a != b,
    ">": lambda a, b: a > b,
    ">=": lambda a, b: a >= b,
    "<": lambda a, b: a < b,
    "<=": lambda a, b: a <= b,
}


def coerce_value(value: tuple) -> object:
    """Best-effort recover a comparable Python value from a mutation's recorded
    ``(kind, repr, type, length)`` rendering. Falls back to the repr string."""
    kind, rep, _type, _length = value
    if kind == "none":
        return None
    if kind == "bool":
        return rep == "True"
    if kind == "int":
        try:
            return int(rep)
        except (TypeError, ValueError):
            return rep  # a giant int rendered as "<int N bits>"
    if kind == "float":
        try:
            return float(rep)
        except (TypeError, ValueError):
            return rep
    if kind == "str":
        return rep if rep is not None else ""
    return rep  # containers/objects: their repr (or None)


def _parse_literal(token: str) -> object:
    t = token.strip()
    if (len(t) >= 2) and t[0] == t[-1] and t[0] in "\"'":
        return t[1:-1]
    low = t.lower()
    if low == "true":
        return True
    if low == "false":
        return False
    if low in ("none", "null"):
        return None
    try:
        return int(t)
    except ValueError:
        pass
    try:
        return float(t)
    except ValueError:
        pass
    return t  # a bare string


def _same_file(a: str, b: str) -> bool:
    return bool(a) and bool(b) and os.path.basename(a) == os.path.basename(b)


def parse_condition(expr: str) -> tuple[str, Callable[[object], bool]]:
    """Parse a watch condition into ``(name, predicate)``.

    Supported: ``name`` or ``name changed`` (fires on any write) and
    ``name <op> literal`` for ``op`` in ``== != > >= < <=`` with an int / float /
    bool / None / quoted-or-bare string literal. Comparisons that raise (e.g.
    ordering a string against a number) are treated as *not* firing, never as an
    error, so a bad condition can't crash a debugging session (P1)."""
    expr = expr.strip()
    for op in (">=", "<=", "==", "!=", ">", "<"):
        idx = expr.find(op)
        if idx != -1:
            name = expr[:idx].strip()
            literal = _parse_literal(expr[idx + len(op) :])
            fn = _OPS[op]

            def pred(value, _fn=fn, _lit=literal):
                try:
                    return bool(_fn(value, _lit))
                except TypeError:
                    return False

            return name, pred
    # bare name (optionally "name changed"): fire on every write
    name = expr.split()[0] if expr.split() else expr
    return name, (lambda _value: True)


# -- breakpoints ------------------------------------------------------------


@dataclass(frozen=True)
class LineBreakpoint:
    file: str  # matched by basename/suffix, so editor abs-paths line up
    line: int

    def matches(self, step: Step) -> bool:
        if step.line != self.line:
            return False
        if not self.file:
            return True
        want = os.path.basename(self.file)
        return os.path.basename(step.file) == want or step.file.endswith(self.file)


@dataclass(frozen=True)
class Watchpoint:
    name: str
    predicate: Callable[[object], bool]
    expr: str = ""

    def matches(self, step: Step) -> bool:
        if step.name != self.name:
            return False
        return self.predicate(coerce_value(step.raw))


# -- the engine -------------------------------------------------------------


class TimeTravel:
    """A navigable cursor over a scope recording's mutation timeline."""

    def __init__(self, recording):
        muts = sorted(recording.mutations, key=lambda m: m.seq)
        self._steps: list[Step] = [
            Step(
                index=i,
                seq=m.seq,
                kind=m.kind,
                name=m.name,
                key=m.key,
                file=m.file,
                qualname=m.qualname,
                line=m.line,
                frame=m.frame,
                value_repr=m.value_repr,
                raw=tuple(m.value),
            )
            for i, m in enumerate(muts)
        ]
        self._pos = len(self._steps)  # start at the end (post-mortem stance)
        self._line_bps: list[LineBreakpoint] = []
        self._watch: list[Watchpoint] = []

    # -- geometry -----------------------------------------------------------

    def __len__(self) -> int:
        return len(self._steps)

    @property
    def steps(self) -> list[Step]:
        return self._steps

    @property
    def pos(self) -> int:
        return self._pos

    def current(self) -> Optional[Step]:
        return self._steps[self._pos - 1] if self._pos > 0 else None

    def at_start(self) -> bool:
        return self._pos == 0

    def at_end(self) -> bool:
        return self._pos == len(self._steps)

    # -- navigation ---------------------------------------------------------

    def goto(self, index: int) -> Optional[Step]:
        """Land the cursor *on* step `index` (0-based, clamped)."""
        if not self._steps:
            return None
        index = max(0, min(index, len(self._steps) - 1))
        self._pos = index + 1
        return self.current()

    def step_forward(self) -> Optional[Step]:
        if self._pos >= len(self._steps):
            return None
        self._pos += 1
        return self.current()

    def step_back(self) -> Optional[Step]:
        if self._pos <= 1:
            self._pos = 0
            return None
        self._pos -= 1
        return self.current()

    def _hits(self, step: Step) -> bool:
        return any(b.matches(step) for b in self._line_bps) or any(
            w.matches(step) for w in self._watch
        )

    def continue_forward(self) -> Optional[Step]:
        """Advance to the next breakpoint after the cursor, else to the end."""
        for j in range(self._pos, len(self._steps)):
            if self._hits(self._steps[j]):
                self._pos = j + 1
                return self.current()
        self._pos = len(self._steps)
        return None

    def continue_back(self) -> Optional[Step]:
        """Reverse to the previous breakpoint before the cursor, else the start."""
        for j in range(self._pos - 2, -1, -1):
            if self._hits(self._steps[j]):
                self._pos = j + 1
                return self.current()
        self._pos = 0
        return None

    # -- state reconstruction ----------------------------------------------

    def state(self) -> dict:
        return self.state_at(self._pos)

    def state_at(self, pos: int) -> dict:
        """Reconstruct the scope's visible state after the first `pos` writes:
        ``{"locals": {name: repr}, "containers": {name: {key: repr}}}``."""
        pos = max(0, min(pos, len(self._steps)))
        loc: dict[str, str] = {}
        cont: dict[str, dict[str, str]] = {}
        for i in range(pos):
            s = self._steps[i]
            if s.kind == "local":
                loc[s.name] = s.value_repr
            else:
                cont.setdefault(s.name, {})[str(s.key)] = s.value_repr
        return {"locals": loc, "containers": cont}

    # -- breakpoints --------------------------------------------------------

    def add_line_breakpoint(self, file: str, line: int) -> LineBreakpoint:
        bp = LineBreakpoint(file, line)
        self._line_bps.append(bp)
        return bp

    def set_line_breakpoints(self, file: str, lines: list[int]) -> list[LineBreakpoint]:
        self._line_bps = [b for b in self._line_bps if not _same_file(b.file, file)]
        bps = [LineBreakpoint(file, ln) for ln in lines]
        self._line_bps.extend(bps)
        return bps

    def clear_line_breakpoints(self) -> None:
        self._line_bps = []

    def add_watchpoint(self, expr: str) -> Watchpoint:
        name, pred = parse_condition(expr)
        wp = Watchpoint(name, pred, expr)
        self._watch.append(wp)
        return wp

    def clear_watchpoints(self) -> None:
        self._watch = []

    # -- breakpoint in the past --------------------------------------------

    def find_all(self, expr: str) -> list[Step]:
        """Every write where the condition holds — the timeline of a value."""
        name, pred = parse_condition(expr)
        out = []
        for s in self._steps:
            if s.name == name and pred(coerce_value(s.raw)):
                out.append(s)
        return out

    def find_first(self, expr: str) -> Optional[Step]:
        """The earliest write matching the condition, and move the cursor to it —
        the "breakpoint in the past". Returns None (cursor unchanged) if never."""
        hits = self.find_all(expr)
        if not hits:
            return None
        self._pos = hits[0].index + 1
        return hits[0]

    def find_last(self, expr: str) -> Optional[Step]:
        hits = self.find_all(expr)
        if not hits:
            return None
        self._pos = hits[-1].index + 1
        return hits[-1]
