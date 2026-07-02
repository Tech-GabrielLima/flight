"""Rendering-independent logic for the TUI viewer (Phase 1.5).

Kept free of any Textual import so it can be unit-tested without a terminal —
the Textual `App` in `_viewer.py` is a thin shell over these functions. All of
this works purely off the reader's `Crash` object (`flight-reader`'s query
surface), never raw bytes (P3).
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from ._read import Crash

# Scalar/leaf kinds — sharing one across frames is not the aliasing insight.
_SCALAR_KINDS = {"none", "bool", "int", "float", "str", "bytes", "redacted", "truncated"}
_IDENT = re.compile(r"[A-Za-z_]\w*")


def frame_locals(crash: "Crash", frame_index: int) -> dict[str, tuple[int, str]]:
    """`name -> (object id, rendered value)` for one frame's locals."""
    out: dict[str, tuple[int, str]] = {}
    for name, oid in crash.frames[frame_index].locals:
        out[name] = (oid, crash.render(oid))
    return out


def inline_values(line_text: str, locals_map: dict[str, tuple[int, str]]) -> list[tuple[str, str]]:
    """Names appearing in `line_text` that are frame locals, with their values —
    the "values inline in the code" feature (TECHNICAL.md §2.2). Order of first
    appearance, de-duplicated."""
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for m in _IDENT.finditer(line_text):
        name = m.group()
        if name in locals_map and name not in seen:
            seen.add(name)
            out.append((name, locals_map[name][1]))
    return out


def alias_index(crash: "Crash") -> dict[int, list[tuple[int, str]]]:
    """`object id -> [(frame index, local name)]` for *reference* objects that
    appear in more than one frame — the "this SAME object is here and here"
    signature feature. Scalars/singletons are excluded (not the insight)."""
    appearances: dict[int, list[tuple[int, str]]] = {}
    for i, fr in enumerate(crash.frames):
        for name, oid in fr.locals:
            appearances.setdefault(oid, []).append((i, name))
    out = {}
    for oid, apps in appearances.items():
        if len(apps) <= 1:
            continue
        node = crash.objects.get(oid)
        if node is not None and node["kind"] in _SCALAR_KINDS:
            continue
        out[oid] = apps
    return out


def object_label(crash: "Crash", oid: int, key: Optional[str] = None) -> str:
    """A short tree label for an object-graph node, optionally prefixed by the
    key/name it is reached through."""
    prefix = f"{key} = " if key is not None else ""
    node = crash.objects.get(oid)
    if node is None:
        return f"{prefix}<missing #{oid}>"
    return f"{prefix}{crash.render(oid)}"


def object_children(crash: "Crash", oid: int) -> list[tuple[Optional[str], int]]:
    """`(key, child id)` edges of an object-graph node (empty for leaves)."""
    node = crash.objects.get(oid)
    return list(node["items"]) if node else []


def has_children(crash: "Crash", oid: int) -> bool:
    node = crash.objects.get(oid)
    return bool(node and node["items"])


def object_detail(crash: "Crash", oid: int) -> list[str]:
    """Human lines describing one node, for the detail panel."""
    node = crash.objects.get(oid)
    if node is None:
        return [f"<missing object #{oid}>"]
    lines = [f"kind    : {node['kind']}"]
    if node.get("type_name"):
        lines.append(f"type    : {node['type_name']}")
    if node.get("repr") is not None:
        lines.append(f"value   : {node['repr']}")
    if node.get("length") is not None:
        lines.append(f"length  : {node['length']}")
    if node.get("truncated"):
        lines.append("(truncated)")
    aliases = alias_index(crash).get(oid)
    if aliases:
        where = ", ".join(f"frame #{i} as {name}" for i, name in aliases)
        lines.append(f"aliased : {where}")
    return lines


def source_window(
    crash: "Crash", frame_index: int, context: int = 6
) -> tuple[list[tuple[int, str, list[tuple[str, str]]]], int]:
    """A window of source lines around a frame's current line, each with its
    inline values. Returns `(rows, current_line)` where a row is
    `(lineno, text, [(name, value), ...])`. Empty rows if the source is absent."""
    fr = crash.frames[frame_index]
    text = crash.sources.get(fr.file)
    if not text:
        return [], fr.lineno
    locals_map = frame_locals(crash, frame_index)
    lines = text.splitlines()
    lo = max(1, fr.lineno - context)
    hi = min(len(lines), fr.lineno + context)
    rows = []
    for n in range(lo, hi + 1):
        line = lines[n - 1]
        rows.append((n, line, inline_values(line, locals_map)))
    return rows, fr.lineno
