"""The object-graph serializer — the most important algorithm of Phase 1.

Serialize arbitrary Python objects into a flat list of nodes, without:
- executing dangerous code uncontrolled (`__repr__` is user code — guarded by a
  global deadline and `safe_repr`);
- looping forever on cycles (identity table);
- losing aliasing (the *same* object reached twice gets the *same* node id — the
  clue "this dict is in frame 3 and frame 9");
- blowing up on giants (per-container/-string limits, depth limit, and a global
  time + byte budget).

It runs **once, at crash time, in a doomed process** (VISION.md §1.1), so it is
written for safety and completeness over speed. Output node tuple shape matches
`flight._core.dump_crash`:

    (id, kind, repr, type_name, length, truncated, [(key_or_None, value_id), ...])
"""

from __future__ import annotations

import time
import types
from collections import deque
from typing import Any

from . import _adapters
from ._scrub import REDACTED, Scrubber

#: Types serialized as repr-only leaves. Their `__dict__` is huge and/or
#: cyclic (a module pulls in `__builtins__`, a class its whole namespace) and is
#: almost never the debugging target — expanding them explodes the graph.
_OPAQUE = (
    types.ModuleType,
    types.FunctionType,
    types.BuiltinFunctionType,
    types.BuiltinMethodType,
    types.MethodType,
    types.CodeType,
    types.FrameType,
    types.TracebackType,
    type,  # classes / metaclasses
)

# Defaults (VISION.md §9). All overridable via Config.
DEADLINE_MS = 250
MAX_BYTES = 20 * 1024 * 1024
MAX_STR = 10 * 1024
MAX_CONTAINER = 200
MAX_DEPTH = 6
REPR_LIMIT = 200

# Node tuple positions, for readers of this file.
# (0:id, 1:kind, 2:repr, 3:type_name, 4:length, 5:truncated, 6:items)
_Node = tuple


class GraphSerializer:
    """Walks live objects into `nodes`, preserving identity, under a budget."""

    def __init__(
        self,
        *,
        deadline_ms: int = DEADLINE_MS,
        max_bytes: int = MAX_BYTES,
        max_str: int = MAX_STR,
        max_container: int = MAX_CONTAINER,
        max_depth: int = MAX_DEPTH,
        repr_limit: int = REPR_LIMIT,
        scrubber: Scrubber | None = None,
    ):
        self.max_str = max_str
        self.max_container = max_container
        self.max_depth = max_depth
        self.repr_limit = repr_limit
        self.scrubber = scrubber or Scrubber()

        self.seen: dict[int, int] = {}  # id(obj) -> node id  (cycles + aliasing)
        self.nodes: list[_Node] = []
        self._next = 0
        self.truncated = False
        self._deadline = time.monotonic() + deadline_ms / 1000.0
        self._bytes_left = max_bytes
        self._queue: deque[tuple[Any, int, int]] = deque()

    # -- public ------------------------------------------------------------

    def add_root(self, obj: Any) -> int:
        """Intern a root object, returning its node id."""
        return self._intern(obj, 0)

    def add_local(self, name: str, value: Any) -> int:
        """Intern a frame local by `(name, value)`, redacting the *value* if the
        name is sensitive (P5) — the local's name is scrubbed just like a dict
        key or attribute would be."""
        if self.scrubber.should_redact(name):
            return self._redacted()
        return self._intern(value, 0)

    def run(self) -> list[_Node]:
        """Drain the work queue into `self.nodes`. Idempotent-ish; call once
        after all roots are added."""
        while self._queue:
            if self._expired():
                self.truncated = True
                break
            obj, nid, depth = self._queue.popleft()
            try:
                node = self._describe(obj, nid, depth)
            except BaseException as e:  # __repr__/attr access is user code (P1)
                node = (nid, "object", f"<describe failed: {type(e).__name__}>", None, None, True, [])
            self.nodes.append(node)
            self._bytes_left -= _size(node)

        # Anything interned but never described (budget hit) still has children
        # pointing at it — emit placeholders so every id resolves.
        if self._queue:
            self.truncated = True
            for _obj, nid, _depth in self._queue:
                self.nodes.append((nid, "truncated", "<truncated>", None, None, True, []))
            self._queue.clear()
        return self.nodes

    # -- interning ---------------------------------------------------------

    def _intern(self, obj: Any, depth: int) -> int:
        key = id(obj)
        existing = self.seen.get(key)
        if existing is not None:
            return existing  # cycle / alias: reuse the id
        nid = self._next
        self._next += 1
        self.seen[key] = nid
        self._queue.append((obj, nid, depth))
        return nid

    def _fresh(self) -> int:
        nid = self._next
        self._next += 1
        return nid

    def _redacted(self) -> int:
        """A fresh node standing in for a scrubbed value (never touches it)."""
        nid = self._fresh()
        self.nodes.append((nid, "redacted", REDACTED, None, None, False, []))
        return nid

    def _expired(self) -> bool:
        return time.monotonic() > self._deadline or self._bytes_left <= 0

    # -- description -------------------------------------------------------

    def _describe(self, obj: Any, nid: int, depth: int) -> _Node:
        t = type(obj)

        # Scalars by exact type (bool before int; bool is an int subclass).
        if obj is None:
            return (nid, "none", "None", None, None, False, [])
        if t is bool:
            return (nid, "bool", "True" if obj else "False", None, None, False, [])
        if t is int:
            return (nid, "int", repr(obj), None, None, False, [])
        if t is float:
            return (nid, "float", repr(obj), None, None, False, [])
        if t is str:
            trunc = len(obj) > self.max_str
            return (nid, "str", obj[: self.max_str], None, len(obj), trunc, [])
        if t in (bytes, bytearray):
            b = bytes(obj)
            return (nid, "bytes", repr(b[:64]), None, len(b), len(b) > 64, [])

        # Opaque leaves: modules, functions, classes, frames… repr only.
        if isinstance(obj, _OPAQUE):
            return (nid, "object", self._safe_repr(obj), _qualname(type(obj)), None, False, [])

        # Registered adapter (numpy/pandas/…): summary + light fields, never data.
        ad = _adapters.resolve(obj)
        if ad is not None:
            try:
                a = ad(obj)
                items = [(str(k), self._intern(v, depth + 1)) for k, v in a.fields.items()]
                return (nid, a.kind, a.summary, _qualname(t), None, False, items)
            except Exception:
                pass  # fall through to the generic paths

        # Past the depth limit: keep a repr, don't expand further.
        if depth >= self.max_depth:
            return (nid, "truncated", self._safe_repr(obj), _qualname(t), None, True, [])

        if isinstance(obj, dict):
            return self._mapping(obj, nid, depth)
        if isinstance(obj, (list, tuple, set, frozenset)):
            return self._sequence(obj, nid, depth)
        return self._object(obj, nid, depth)

    def _mapping(self, obj: Any, nid: int, depth: int) -> _Node:
        items: list[tuple[str, int]] = []
        real_len = _safe_len(obj)
        for i, (k, v) in enumerate(_safe_items(obj)):
            if i >= self.max_container:
                break
            key = self._keystr(k)
            child = self._redacted() if self.scrubber.should_redact(k) else self._intern(v, depth + 1)
            items.append((key, child))
        kind = "dict"
        type_name = _qualname(type(obj)) if type(obj) is not dict else None
        return (nid, kind, None, type_name, real_len, real_len > self.max_container, items)

    def _sequence(self, obj: Any, nid: int, depth: int) -> _Node:
        kinds = {list: "list", tuple: "tuple", set: "set", frozenset: "frozenset"}
        kind = kinds.get(type(obj), "list" if isinstance(obj, (list, tuple)) else "set")
        real_len = _safe_len(obj)
        items: list[tuple[None, int]] = []
        for i, v in enumerate(obj):
            if i >= self.max_container:
                break
            items.append((None, self._intern(v, depth + 1)))
        type_name = _qualname(type(obj)) if type(obj) not in kinds else None
        return (nid, kind, None, type_name, real_len, real_len > self.max_container, items)

    def _object(self, obj: Any, nid: int, depth: int) -> _Node:
        attrs = _get_attrs(obj)
        real_len = len(attrs)
        items: list[tuple[str, int]] = []
        for i, (name, value) in enumerate(attrs.items()):
            if i >= self.max_container:
                break
            child = self._redacted() if self.scrubber.should_redact(name) else self._intern(value, depth + 1)
            items.append((str(name), child))
        return (nid, "object", self._safe_repr(obj), _qualname(type(obj)), real_len,
                real_len > self.max_container, items)

    # -- helpers -----------------------------------------------------------

    def _safe_repr(self, obj: Any) -> str:
        try:
            r = repr(obj)
        except BaseException as e:  # __repr__ is user code
            return f"<repr failed: {type(e).__name__}>"
        return r if len(r) <= self.repr_limit else r[: self.repr_limit] + "…"

    def _keystr(self, k: Any) -> str:
        if isinstance(k, str):
            return k if len(k) <= self.repr_limit else k[: self.repr_limit] + "…"
        return self._safe_repr(k)


def _qualname(t: type) -> str:
    return f"{t.__module__}.{t.__qualname__}"


def _size(node: _Node) -> int:
    """Rough byte cost of a node, for the budget."""
    rep = node[2] or ""
    return 24 + len(rep) + 16 * len(node[6])


def _safe_len(obj: Any) -> int:
    try:
        return len(obj)
    except Exception:
        return 0


def _safe_items(obj: Any):
    try:
        return list(obj.items())
    except Exception:
        return []


def _get_attrs(obj: Any) -> dict[str, Any]:
    """Best-effort attribute snapshot from `__dict__` and `__slots__`."""
    out: dict[str, Any] = {}
    d = getattr(obj, "__dict__", None)
    if isinstance(d, dict):
        out.update(d)
    slots = getattr(type(obj), "__slots__", None)
    if slots:
        if isinstance(slots, str):
            slots = (slots,)
        for s in slots:
            try:
                out[s] = getattr(obj, s)
            except Exception:
                pass
    return out
