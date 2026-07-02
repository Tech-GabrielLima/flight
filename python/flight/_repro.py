"""Phase 3, rung 1 — shallow reproduction (TECHNICAL.md §4.3).

Given a crash `.flight`, generate a **self-contained, runnable** `repro_bug.py`
that rebuilds the crash function's arguments from the object graph, imports the
function from the (embedded) source, calls it, and asserts the same exception.
Then *verify* it by running it in a subprocess: only a script that actually
reproduces is labelled verified — the bug report that writes and checks itself.

Honest scope: this reproduces bugs that depend on the function's arguments /
local state (a large class of logic bugs). Values are the crash-time snapshots
(a reassigned parameter shows its crash-time value); opaque objects become
attribute-only stubs; truncated/redacted values can't be reconstructed exactly
(the result is then flagged *approximate*). Bugs that depend on non-determinism
(time, randomness, I/O) need the recorded NONDET tape — see [`_nondet`]; when the
`.flight` carries one, the generated repro replays it too.
"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from ._read import read

_SCALAR_KINDS = {"int", "float", "bool", "none", "str", "bytes"}


@dataclass
class ReproResult:
    script: str
    verified: Optional[bool] = None  # None = not run
    approximate: bool = False
    reason: str = ""
    path: Optional[Path] = None
    notes: list[str] = field(default_factory=list)


class _Reconstructor:
    """Emits Python that rebuilds an object graph, preserving aliasing and
    surviving cycles (mutable containers are created empty, then filled)."""

    def __init__(self, objects: dict[int, dict]):
        self.objects = objects
        self.lines: list[str] = []
        self.var_of: dict[int, str] = {}
        self.approximate = False
        self.notes: list[str] = []

    def build(self, oid: int) -> str:
        """Return a Python expression evaluating to the reconstructed object."""
        node = self.objects.get(oid)
        if node is None:
            self.approximate = True
            return "None  # <missing object>"
        kind = node["kind"]
        if kind in _SCALAR_KINDS:
            return self._scalar(node)
        if oid in self.var_of:
            return self.var_of[oid]  # already built / being built (cycle/alias)
        var = f"_v{len(self.var_of)}"
        self.var_of[oid] = var
        try:
            self._build_container(var, kind, node)
        except Exception:
            self.approximate = True
            self.lines.append(f"{var} = None  # failed to reconstruct {kind}")
        return var

    def _build_container(self, var: str, kind: str, node: dict) -> None:
        items = node.get("items", [])
        if kind == "dict":
            self.lines.append(f"{var} = {{}}")
            for k, cid in items:
                self.lines.append(f"{var}[{_key_literal(k)}] = {self.build(cid)}")
        elif kind == "list":
            self.lines.append(f"{var} = []")
            for _k, cid in items:
                self.lines.append(f"{var}.append({self.build(cid)})")
        elif kind == "set":
            self.lines.append(f"{var} = set()")
            for _k, cid in items:
                self.lines.append(f"{var}.add({self.build(cid)})")
        elif kind in ("tuple", "frozenset"):
            elems = [self.build(cid) for _k, cid in items]
            ctor = "tuple" if kind == "tuple" else "frozenset"
            self.lines.append(f"{var} = {ctor}([{', '.join(elems)}])")
        elif kind == "object":
            cls = node.get("type_name") or "object"
            self.lines.append(f"{var} = _Stub({cls!r})")
            for k, cid in items:
                if k is not None:
                    self.lines.append(f"setattr({var}, {k!r}, {self.build(cid)})")
            self.notes.append(f"{cls} rebuilt as an attribute-only stub")
            # A stub carries the attributes but not the real type or methods.
            self.approximate = True
        else:
            # ndarray / dataframe / adapter / redacted / truncated
            self.approximate = True
            self.lines.append(f"{var} = None  # {kind} not reconstructable")

    def _scalar(self, node: dict) -> str:
        kind = node["kind"]
        rep = node.get("repr")
        if kind == "none":
            return "None"
        if kind == "bool":
            return "True" if rep == "True" else "False"
        if kind == "int":
            return rep if rep and _looks_int(rep) else f"int({rep!r})"
        if kind == "float":
            if rep in ("inf", "-inf", "nan"):
                return f"float({rep!r})"
            return rep if rep else "0.0"
        if kind == "str":
            if node.get("truncated"):
                self.approximate = True
            return repr(rep if rep is not None else "")
        if kind == "bytes":
            if node.get("truncated"):
                self.approximate = True
            return rep if rep and rep.startswith(("b'", 'b"')) else "b''"
        return "None"


def _looks_int(s: str) -> bool:
    try:
        int(s)
        return True
    except ValueError:
        return False


def _key_literal(k) -> str:
    """Best-effort literal for a dict key (keys were stringified at capture)."""
    if k is None:
        return "None"
    if _looks_int(k):
        return k
    if k in ("True", "False", "None"):
        return k
    return repr(k)


_PREAMBLE = '''\
# ---------------------------------------------------------------------------
# Auto-generated by `flight repro`. Self-contained: rebuilds the crash frame's
# state and re-runs the failing function to reproduce the exception.
# ---------------------------------------------------------------------------
import sys as _sys
import types as _types
import inspect as _inspect


class _Stub:
    """Stand-in for an object flight could not fully reconstruct: it carries
    the captured attributes, enough for logic that only reads them."""

    def __init__(self, _cls="object"):
        object.__setattr__(self, "_flight_class", _cls)

    def __repr__(self):
        return f"<flight stub {object.__getattribute__(self, '_flight_class')}>"
'''


def _render_script(
    *, source: str, file: str, qualname: str, exc_type: str, build_lines: list[str],
    local_names: list[str], tape_json: Optional[str],
) -> str:
    parts = [_PREAMBLE, ""]

    # The crash function's source, embedded and exec'd into a private module.
    parts.append(f"_SRC = {json.dumps(source)}")
    parts.append(f"_FILE = {json.dumps(file)}")
    parts.append("_mod = _types.ModuleType('_flight_crash')")
    parts.append("_mod.__file__ = _FILE")
    parts.append("# Unguarded top-level code may crash on import; that's fine — the")
    parts.append("# function we need was already defined above it.")
    parts.append("try:")
    parts.append("    exec(compile(_SRC, _FILE, 'exec'), _mod.__dict__)")
    parts.append("except BaseException:")
    parts.append("    pass")
    parts.append("")

    # Rebuild the crash frame's locals.
    parts.append("# --- reconstructed crash-frame state ---")
    parts.extend(build_lines)
    parts.append("_locals = {")
    for name in local_names:
        parts.append(f"    {name!r}: {name}_ref,")
    parts.append("}")
    parts.append("")

    # Optional: replay the recorded non-determinism around the call.
    if tape_json is not None:
        parts.append("# --- recorded non-determinism (deterministic replay) ---")
        parts.append("try:")
        parts.append("    import flight as _flight")
        parts.append(f"    _tape = _flight.Tape.from_json({tape_json!r})")
        parts.append("except Exception:")
        parts.append("    _tape = None")
        parts.append("")

    # Resolve and call the function.
    parts.append(f"_qualname = {json.dumps(qualname)}")
    parts.append("_fn = _mod")
    parts.append("for _part in _qualname.split('.'):")
    parts.append("    if _part == '<locals>':")
    parts.append("        _fn = None; break")
    parts.append("    _fn = getattr(_fn, _part, None)")
    parts.append("    if _fn is None: break")
    parts.append("")
    parts.append("if _fn is None or not callable(_fn):")
    parts.append("    print('FLIGHT_REPRO_UNRESOLVED', _qualname); _sys.exit(3)")
    parts.append("")
    parts.append("_params = [p.name for p in _inspect.signature(_fn).parameters.values()")
    parts.append("           if p.kind in (p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY)]")
    parts.append("_args = {k: _locals[k] for k in _params if k in _locals}")
    parts.append("")
    parts.append(f"_expected = {json.dumps(exc_type.split('.')[-1])}")
    parts.append("")
    parts.append("def _invoke():")
    parts.append("    return _fn(**_args)")
    parts.append("")
    parts.append("def _attempt():")
    if tape_json is not None:
        parts.append("    if _tape is None:")
        parts.append("        _invoke(); return")
        parts.append("    # Replay the recorded non-determinism, re-invoking until the tape")
        parts.append("    # drives the code to the recorded failure (handles a loop that")
        parts.append("    # crashes only on iteration N — the crash draw is the tape's tail).")
        parts.append("    for _ in range(len(_tape) + 1):")
        parts.append("        try:")
        parts.append("            _flight.replay_tape(_tape, _invoke)")
        parts.append("        except _flight.ReplayDivergence:")
        parts.append("            return")
    else:
        parts.append("    _invoke()")
    parts.append("")
    parts.append("try:")
    parts.append("    _attempt()")
    parts.append("except BaseException as _e:")
    parts.append("    if type(_e).__name__ == _expected:")
    parts.append("        print('FLIGHT_REPRO_OK', _expected); _sys.exit(0)")
    parts.append("    print('FLIGHT_REPRO_DIFF', type(_e).__name__, 'expected', _expected)")
    parts.append("    raise")
    parts.append("print('FLIGHT_REPRO_NOEXC'); _sys.exit(1)")
    return "\n".join(parts) + "\n"


def build_repro(flight_path, *, include_tape: bool = True) -> ReproResult:
    """Generate (but do not write) a repro script for a crash `.flight`."""
    fl = read(flight_path)
    if not fl.has_crash:
        return ReproResult(script="", verified=False, reason="no crash frames in this file")
    crash = fl.crash()
    if not crash.frames:
        return ReproResult(script="", verified=False, reason="no frames captured")
    frame = crash.frames[0]
    source = crash.sources.get(frame.file)
    if not source:
        return ReproResult(
            script="", verified=False, reason=f"source not captured for {frame.file}"
        )

    rec = _Reconstructor(crash.objects)
    ref_lines: list[str] = []
    local_names: list[str] = []
    for name, oid in frame.locals:
        if name.startswith("__") and name.endswith("__"):
            continue
        expr = rec.build(oid)  # appends any container-build lines to rec.lines
        ref_lines.append(f"{name}_ref = {expr}")
        local_names.append(name)
    # Container/object builds must be emitted before the *_ref aliases.
    build_lines = rec.lines + ref_lines

    exc_type = crash.exceptions[0][0] if crash.exceptions else "Exception"
    tape_json = None
    if include_tape and fl.has_nondet:
        tape_json = fl.tape_json()

    script = _render_script(
        source=source,
        file=frame.file,
        qualname=frame.qualname,
        exc_type=exc_type,
        build_lines=build_lines,
        local_names=local_names,
        tape_json=tape_json,
    )
    return ReproResult(
        script=script,
        approximate=rec.approximate,
        reason="generated",
        notes=list(dict.fromkeys(rec.notes)),
    )


def write_repro(flight_path, out_path=None, *, verify: bool = True) -> ReproResult:
    """Generate a repro script, write it, and (optionally) verify it runs and
    reproduces the exception in a subprocess."""
    result = build_repro(flight_path)
    if not result.script:
        return result
    out = Path(out_path) if out_path else Path("repro_bug.py")
    out.write_text(result.script)
    result.path = out
    if verify:
        result.verified = _verify(out)
    return result


def _verify(script_path: Path) -> bool:
    try:
        proc = subprocess.run(
            [sys.executable, str(script_path)],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except Exception:
        return False
    return proc.returncode == 0 and "FLIGHT_REPRO_OK" in proc.stdout
