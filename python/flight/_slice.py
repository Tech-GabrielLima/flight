from __future__ import annotations

import ast
import os
from dataclasses import dataclass, field
from typing import Optional

_OPAQUE_KINDS = {"object", "adapter", "ndarray", "dataframe", "redacted", "truncated"}

_MAX_HOPS_DEFAULT = 32


@dataclass
class Hop:

    frame: int
    qualname: str
    file: str
    line: int
    var: str
    object_id: Optional[int]
    reason: str
    detail: str
    value: str = ""
    source_line: str = ""

    @property
    def where(self) -> str:
        return f"{os.path.basename(self.file)}:{self.line}" if self.file else "?"


@dataclass
class Slice:

    frame: int
    var: str
    value: str
    hops: list[Hop] = field(default_factory=list)
    root: str = ""
    truncated: bool = False

    def __bool__(self) -> bool:
        return bool(self.hops)

    def __len__(self) -> int:
        return len(self.hops)

    def render(self) -> str:
        if not self.hops:
            return f"{self.var}: nothing to slice (no such local, or no detail captured)"
        head = f"{self.var} ({self.value}) — how this value came to be:"
        lines = [head]
        for hop in self.hops:
            glyph = _GLYPH.get(hop.reason, "  ")
            if hop.reason in ("seed", "param", "write", "read-of"):
                loc = f"{hop.qualname}  {hop.where}"
                src = f"   {hop.source_line.strip()}" if hop.source_line else ""
                lines.append(f"  #{hop.frame} {loc}{src}")
                if hop.detail:
                    lines.append(f"        {glyph} {hop.detail}")
            else:
                lines.append(f"       {glyph} {hop.detail}")
        if self.truncated:
            lines.append("  … (more hops — raise --max-hops to keep going)")
        if self.root:
            lines.append(f"  ⇒ root: {self.root}")
        return "\n".join(lines)


_GLYPH = {
    "seed": "•",
    "param": "•",
    "write": "•",
    "read-of": "←",
    "alias": "↔",
    "contained-in": "↰",
    "opaque": "×",
    "root": "⇒",
}


def _containers_of(objects: dict, oid: int) -> list[tuple[int, object]]:
    out: list[tuple[int, object]] = []
    for cid, node in objects.items():
        for key, child in node.get("items", []):
            if child == oid:
                out.append((cid, key))
    return out


def _local_names_for(crash, oid: int) -> list[tuple[int, str]]:
    return crash.aliases(oid)


def _find_function(tree: ast.AST, first_lineno: int, current_line: int):
    exact = None
    span: Optional[tuple[int, ast.AST]] = None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            if getattr(node, "lineno", None) == first_lineno:
                exact = node
            end = getattr(node, "end_lineno", None)
            if end is not None and node.lineno <= current_line <= end:
                size = end - node.lineno
                if span is None or size < span[0]:
                    span = (size, node)
    if exact is not None:
        return exact
    return span[1] if span is not None else tree


def _params(func) -> set[str]:
    if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
        return set()
    a = func.args
    names = {p.arg for p in (*a.posonlyargs, *a.args, *a.kwonlyargs)}
    if a.vararg:
        names.add(a.vararg.arg)
    if a.kwarg:
        names.add(a.kwarg.arg)
    return names


def _names_read(node: ast.AST) -> list[str]:
    seen: dict[str, None] = {}
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Load):
            seen.setdefault(sub.id, None)
    return list(seen)


def _binds(stmt: ast.AST, var: str) -> bool:
    targets: list[ast.AST] = []
    if isinstance(stmt, ast.Assign):
        targets = list(stmt.targets)
    elif isinstance(stmt, (ast.AnnAssign, ast.AugAssign)):
        targets = [stmt.target]
    elif isinstance(stmt, (ast.For, ast.AsyncFor)):
        targets = [stmt.target]
    elif isinstance(stmt, (ast.With, ast.AsyncWith)):
        targets = [it.optional_vars for it in stmt.items if it.optional_vars]
    else:
        return False
    for t in targets:
        for sub in ast.walk(t):
            if isinstance(sub, ast.Name) and sub.id == var:
                return True
    return False


def _rhs_of(stmt: ast.AST) -> Optional[ast.AST]:
    if isinstance(stmt, (ast.Assign, ast.AnnAssign)):
        return stmt.value
    if isinstance(stmt, ast.AugAssign):
        return stmt.value
    if isinstance(stmt, (ast.For, ast.AsyncFor)):
        return stmt.iter
    if isinstance(stmt, (ast.With, ast.AsyncWith)):
        return stmt
    return None


@dataclass
class _Binding:
    line: int
    source: str
    reads: list[str]
    is_param: bool


def _last_binding(
    source: str, first_lineno: int, current_line: int, var: str, executed: Optional[set[int]]
) -> Optional[_Binding]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    func = _find_function(tree, first_lineno, current_line)
    if var in _params(func):
        return _Binding(line=first_lineno, source="", reads=[], is_param=True)

    candidates: list[ast.AST] = []
    for stmt in ast.walk(func):
        if isinstance(stmt, (ast.stmt,)) and _binds(stmt, var):
            ln = getattr(stmt, "lineno", 0)
            if ln <= current_line:
                candidates.append(stmt)
    if not candidates:
        return None

    def ran(stmt) -> bool:
        return executed is None or getattr(stmt, "lineno", -1) in executed

    ran_ones = [s for s in candidates if ran(s)]
    pool = ran_ones or candidates
    best = max(pool, key=lambda s: getattr(s, "lineno", 0))
    rhs = _rhs_of(best)
    reads = [n for n in _names_read(rhs) if n != var] if rhs is not None else []
    src_lines = source.splitlines()
    ln = getattr(best, "lineno", 0)
    src = src_lines[ln - 1] if 0 < ln <= len(src_lines) else ""
    return _Binding(line=ln, source=src, reads=reads, is_param=False)


def _executed_lines(flight, file: str) -> Optional[set[int]]:
    try:
        events = flight.events(limit=5000)
    except Exception:
        return None
    base = os.path.basename(file)
    lines = {ln for _kind, f, _qual, ln in events if os.path.basename(f) == base and ln}
    return lines or None


def backward_slice(flight, frame: int = 0, var: str = "", *, max_hops: int = _MAX_HOPS_DEFAULT):
    from ._read import read

    if not hasattr(flight, "crash"):
        flight = read(flight)

    try:
        crash = flight.crash()
    except Exception:
        return Slice(frame=frame, var=var, value="<unreadable>")
    if not crash.frames or not (0 <= frame < len(crash.frames)):
        return Slice(frame=frame, var=var, value="<no such frame>")

    recording = None
    if flight.has_mutations:
        try:
            recording = flight.recording()
        except Exception:
            recording = None

    fr = crash.frames[frame]
    seed_oid = next((oid for name, oid in fr.locals if name == var), None)
    if seed_oid is None:
        return Slice(frame=frame, var=var, value="<no such local>")

    sl = Slice(frame=frame, var=var, value=crash.render(seed_oid))
    visited_oids: set[int] = set()
    visited_frame_names: set[tuple[int, str]] = set()

    cur_frame, cur_var, cur_oid = frame, var, seed_oid
    hops = 0
    while True:
        if hops >= max_hops:
            sl.truncated = True
            break
        hops += 1
        f = crash.frames[cur_frame]
        node = crash.node(cur_oid)
        value = crash.render(cur_oid)

        binding = None
        if recording is not None:
            binding = _scope_producer(recording, cur_frame, cur_var)
        if binding is None:
            src = crash.sources.get(f.file)
            executed = _executed_lines(flight, f.file) if src else None
            binding = _last_binding(src, f.first_lineno, f.lineno, cur_var, executed) if src else None

        reason = "seed" if hops == 1 else "write"
        detail = ""
        source_line = ""
        if binding is not None and binding.is_param:
            reason = "param"
            detail = "parameter — its value comes from the caller (aliased below)"
        elif binding is not None:
            reason = "seed" if hops == 1 else "write"
            source_line = binding.source
            if binding.reads:
                detail = "computed from " + ", ".join(sorted(binding.reads))
        sl.hops.append(
            Hop(
                frame=cur_frame,
                qualname=f.qualname,
                file=f.file,
                line=binding.line if binding is not None else f.lineno,
                var=cur_var,
                object_id=cur_oid,
                reason=reason,
                detail=detail,
                value=value,
                source_line=source_line,
            )
        )
        visited_oids.add(cur_oid)
        visited_frame_names.add((cur_frame, cur_var))

        aliases = [
            (fi, nm)
            for fi, nm in _local_names_for(crash, cur_oid)
            if (fi, nm) != (cur_frame, cur_var) and (fi, nm) not in visited_frame_names
        ]
        for fi, nm in aliases:
            qn = crash.frames[fi].qualname
            sl.hops.append(
                Hop(
                    frame=fi, qualname=qn, file=crash.frames[fi].file,
                    line=crash.frames[fi].lineno, var=nm, object_id=cur_oid,
                    reason="alias",
                    detail=f"the SAME object as '{nm}' in {qn} (frame #{fi})",
                )
            )
            visited_frame_names.add((fi, nm))

        if node is not None and node.get("kind") in _OPAQUE_KINDS:
            sl.hops.append(
                Hop(cur_frame, f.qualname, f.file, f.lineno, cur_var, cur_oid,
                    "opaque", f"<opaque {node.get('kind')}> — provenance not followable")
            )
            break

        containers = [c for c, _k in _containers_of(crash.objects, cur_oid) if c not in visited_oids]
        if containers:
            cid = containers[0]
            key = next(k for c, k in _containers_of(crash.objects, cur_oid) if c == cid)
            owner = _local_names_for(crash, cid)
            owner_desc = ""
            if owner:
                ofi, onm = owner[0]
                owner_desc = f" in {crash.frames[ofi].qualname} (frame #{ofi})"
            cval = crash.render(cid)
            sl.hops.append(
                Hop(cur_frame, f.qualname, f.file, f.lineno, cur_var, cid,
                    "contained-in",
                    f"is {_owner_name(owner, crash)}[{_key_repr(key)}]{owner_desc}  ({cval})")
            )
            if owner:
                cur_frame, cur_var = owner[0]
            cur_oid = cid
            continue

        if binding is not None and binding.reads:
            nxt = _pick_read(crash, cur_frame, binding.reads, visited_frame_names)
            if nxt is not None:
                cur_var, cur_oid = nxt
                sl.hops.append(
                    Hop(cur_frame, f.qualname, f.file, binding.line, cur_var, cur_oid,
                        "read-of", f"'{cur_var}' was read to produce it")
                )
                continue

        break

    sl.root = _describe_root(crash, cur_oid, cur_frame, cur_var)
    return sl


def _owner_name(owner, crash) -> str:
    if owner:
        return owner[0][1]
    return "<container>"


def _key_repr(key) -> str:
    if key is None:
        return ""
    return repr(key) if not (isinstance(key, str) and key.isidentifier()) else repr(key)


def _pick_read(crash, frame_idx: int, reads: list[str], visited) -> Optional[tuple[str, int]]:
    fr = crash.frames[frame_idx]
    locs = dict(fr.locals)
    for nm in reads:
        if nm in locs and (frame_idx, nm) not in visited:
            return nm, locs[nm]
    return None


def _describe_root(crash, oid: int, frame_idx: int, var: str) -> str:
    val = crash.render(oid)
    names = crash.aliases(oid)
    if names:
        fi, nm = max(names, key=lambda fn: fn[0])
        return f"'{nm}' was {val} in {crash.frames[fi].qualname} (frame #{fi})"
    containers = _containers_of(crash.objects, oid)
    if containers:
        cid, key = containers[0]
        owner = crash.aliases(cid)
        owner_desc = f"{owner[0][1]}[{_key_repr(key)}]" if owner else f"a container[{_key_repr(key)}]"
        return f"{owner_desc} was {val}"
    return f"'{var}' was {val}"


def _scope_producer(recording, frame_idx: int, var: str) -> Optional[_Binding]:
    writes = [m for m in recording.history(var)]
    if not writes:
        return None
    last = writes[-1]
    return _Binding(line=last.line, source="", reads=[], is_param=False)
