from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Callable, Optional


@dataclass(frozen=True)
class Step:

    index: int
    seq: int
    kind: str
    name: str
    key: Optional[str]
    file: str
    qualname: str
    line: int
    frame: int
    value_repr: str
    raw: tuple

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


_OPS: dict[str, Callable[[object, object], bool]] = {
    "==": lambda a, b: a == b,
    "!=": lambda a, b: a != b,
    ">": lambda a, b: a > b,
    ">=": lambda a, b: a >= b,
    "<": lambda a, b: a < b,
    "<=": lambda a, b: a <= b,
}


def coerce_value(value: tuple) -> object:
    kind, rep, _type, _length = value
    if kind == "none":
        return None
    if kind == "bool":
        return rep == "True"
    if kind == "int":
        try:
            return int(rep)
        except (TypeError, ValueError):
            return rep
    if kind == "float":
        try:
            return float(rep)
        except (TypeError, ValueError):
            return rep
    if kind == "str":
        return rep if rep is not None else ""
    return rep


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
    return t


def _same_file(a: str, b: str) -> bool:
    return bool(a) and bool(b) and os.path.basename(a) == os.path.basename(b)


def parse_len(expr: str) -> Optional[tuple[str, Callable[[int, int], bool], int]]:
    e = expr.strip()
    if not (e.startswith("len(") or e.startswith("size(")):
        return None
    open_paren = e.index("(")
    close = e.find(")", open_paren)
    if close == -1:
        return None
    name = e[open_paren + 1 : close].strip()
    rest = e[close + 1 :].strip()
    for op in (">=", "<=", "==", "!=", ">", "<"):
        if rest.startswith(op):
            try:
                return name, _OPS[op], int(rest[len(op) :].strip())
            except ValueError:
                return None
    return None


def parse_condition(expr: str) -> tuple[str, Callable[[object], bool]]:
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
    name = expr.split()[0] if expr.split() else expr
    return name, (lambda _value: True)


@dataclass(frozen=True)
class LineBreakpoint:
    file: str
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


class TimeTravel:

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
        self._pos = len(self._steps)
        self._line_bps: list[LineBreakpoint] = []
        self._watch: list[Watchpoint] = []


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


    def goto(self, index: int) -> Optional[Step]:
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
        for j in range(self._pos, len(self._steps)):
            if self._hits(self._steps[j]):
                self._pos = j + 1
                return self.current()
        self._pos = len(self._steps)
        return None

    def continue_back(self) -> Optional[Step]:
        for j in range(self._pos - 2, -1, -1):
            if self._hits(self._steps[j]):
                self._pos = j + 1
                return self.current()
        self._pos = 0
        return None


    def state(self) -> dict:
        return self.state_at(self._pos)

    def state_at(self, pos: int) -> dict:
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


    def find_all(self, expr: str) -> list[Step]:
        size_q = parse_len(expr)
        if size_q is not None:
            name, op, n = size_q
            keys: set = set()
            out = []
            for s in self._steps:
                if s.kind in ("item", "attr") and s.name == name:
                    keys.add(s.key)
                    if op(len(keys), n):
                        out.append(s)
            return out
        name, pred = parse_condition(expr)
        out = []
        for s in self._steps:
            if s.name == name and pred(coerce_value(s.raw)):
                out.append(s)
        return out

    def find_first(self, expr: str) -> Optional[Step]:
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
