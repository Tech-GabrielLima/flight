from __future__ import annotations

import ast
import difflib
import os
from dataclasses import dataclass, field
from typing import Callable, Optional

from ._nondet import ReplayDivergence
from ._read import read

VERIFIED = "VERIFIED"
CHANGES_BEHAVIOR = "CHANGES_BEHAVIOR"
REJECTED = "REJECTED"
NO_PATCH = "NO_PATCH"

_EMPTYABLE = {"list", "dict", "tuple", "set", "frozenset", "str", "bytes"}


class AgentTools:

    def __init__(self, flight_path):
        self.flight = read(flight_path)
        self.crash = self.flight.crash()


    def frames(self) -> list[dict]:
        return [
            {"index": i, "qualname": f.qualname, "file": f.file, "line": f.lineno}
            for i, f in enumerate(self.crash.frames)
        ]

    def locals(self, frame: int = 0) -> list[dict]:
        if not (0 <= frame < len(self.crash.frames)):
            return []
        out = []
        for name, oid in self.crash.frames[frame].locals:
            if name.startswith("__") and name.endswith("__"):
                continue
            node = self.crash.node(oid)
            out.append(
                {
                    "name": name,
                    "object_id": oid,
                    "value": self.crash.render(oid),
                    "kind": node.get("kind") if node else None,
                    "length": node.get("length") if node else None,
                }
            )
        return out

    def object(self, oid: int) -> Optional[dict]:
        return self.crash.node(oid)

    def aliases(self, oid: int) -> list[tuple[int, str]]:
        return self.crash.aliases(oid)

    def timeline(self, var: str) -> list[str]:
        if not self.flight.has_mutations:
            return []
        return [m.value_repr for m in self.flight.recording().history(var)]

    def why(self, frame: int = 0, var: str = "") -> str:
        from ._slice import backward_slice

        return backward_slice(self.flight, frame=frame, var=var).render()

    def source(self, file: Optional[str] = None) -> Optional[str]:
        if file is None:
            file = self.crash.frames[0].file if self.crash.frames else None
        return self.crash.sources.get(file) if file else None


    @property
    def exc_type(self) -> str:
        return self.crash.exceptions[0][0].split(".")[-1] if self.crash.exceptions else "Exception"

    @property
    def crash_frame(self):
        return self.crash.frames[0] if self.crash.frames else None


def apply_unified_diff(original: str, diff: str) -> Optional[str]:
    orig_lines = original.splitlines(keepends=False)
    out: list[str] = []
    src = 0
    it = iter(diff.splitlines())
    in_hunk = False
    for raw in it:
        if raw.startswith("--- ") or raw.startswith("+++ "):
            continue
        if raw.startswith("@@"):
            try:
                seg = raw.split("@@")[1].strip()
                minus = [p for p in seg.split() if p.startswith("-")][0]
                start = int(minus[1:].split(",")[0]) - 1
            except (IndexError, ValueError):
                return None
            if start < 0:
                start = 0
            out.extend(orig_lines[src:start])
            src = start
            in_hunk = True
            continue
        if not in_hunk:
            continue
        if raw.startswith(" "):
            if src >= len(orig_lines) or orig_lines[src] != raw[1:]:
                return None
            out.append(orig_lines[src])
            src += 1
        elif raw.startswith("-"):
            if src >= len(orig_lines) or orig_lines[src] != raw[1:]:
                return None
            src += 1
        elif raw.startswith("+"):
            out.append(raw[1:])
        else:
            return None
    out.extend(orig_lines[src:])
    trailing = "\n" if original.endswith("\n") else ""
    return "\n".join(out) + trailing


def _unified(original: str, patched: str, path: str) -> str:
    base = os.path.basename(path) or "source"
    return "".join(
        difflib.unified_diff(
            original.splitlines(keepends=True),
            patched.splitlines(keepends=True),
            fromfile=f"a/{base}",
            tofile=f"b/{base}",
        )
    )


def _find_suspect(crash, frame) -> Optional[str]:
    empties, nones, zeros = [], [], []
    for name, oid in frame.locals:
        if name.startswith("__") and name.endswith("__"):
            continue
        node = crash.node(oid)
        if node is None:
            continue
        kind = node.get("kind")
        if kind in _EMPTYABLE and node.get("length") == 0:
            empties.append(name)
        elif kind == "none":
            nones.append(name)
        elif kind == "int" and node.get("repr") == "0":
            zeros.append(name)
    for pool in (empties, nones, zeros):
        if pool:
            return pool[0]
    return None


def _find_funcdef(tree: ast.AST, first_lineno: int):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.lineno == first_lineno:
                return node
    best = None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            end = getattr(node, "end_lineno", node.lineno)
            if node.lineno <= first_lineno <= end:
                if best is None or node.lineno > best.lineno:
                    best = node
    return best


def heuristic_patch(tools: AgentTools, feedback: str = "") -> Optional[str]:
    frame = tools.crash_frame
    if frame is None:
        return None
    source = tools.source(frame.file)
    if not source:
        return None
    suspect = _find_suspect(tools.crash, frame)
    if suspect is None:
        return None
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    func = _find_funcdef(tree, frame.first_lineno)
    if func is None or not func.body:
        return None

    body0 = func.body[0]
    indent = body0.col_offset
    default = "0.0" if tools.exc_type == "ZeroDivisionError" else "None"
    pad = " " * indent
    guard = [f"{pad}if not {suspect}:", f"{pad}    return {default}"]

    lines = source.splitlines(keepends=False)
    insert_at = body0.lineno - 1
    if not (0 <= insert_at <= len(lines)):
        return None
    patched_lines = lines[:insert_at] + guard + lines[insert_at:]
    patched = "\n".join(patched_lines) + ("\n" if source.endswith("\n") else "")
    return _unified(source, patched, frame.file)


@dataclass
class Verification:
    status: str
    baseline: str
    counterfactual: str


def _build_invocable_from_source(flight, patched_source: str):
    from ._repro import _PREAMBLE, _Reconstructor

    crash = flight.crash()
    if not crash.frames:
        return None
    frame = crash.frames[0]

    mod_ns: dict = {}
    try:
        exec(compile(patched_source, frame.file, "exec"), mod_ns)
    except BaseException:
        pass

    rec = _Reconstructor(crash.objects)
    ref_lines, names = [], []
    for name, oid in frame.locals:
        if name.startswith("__") and name.endswith("__"):
            continue
        ref_lines.append(f"{name}_ref = {rec.build(oid)}")
        names.append(name)
    build = _PREAMBLE + "\n" + "\n".join(rec.lines + ref_lines)
    build_ns: dict = {}
    try:
        exec(compile(build, "<flight-reconstruct>", "exec"), build_ns)
    except BaseException:
        return None
    locals_map = {n: build_ns.get(f"{n}_ref") for n in names}

    fn = mod_ns
    for part in frame.qualname.split("."):
        if part == "<locals>":
            return None
        fn = mod_ns.get(part) if fn is mod_ns else getattr(fn, part, None)
        if fn is None:
            return None
    if not callable(fn):
        return None

    import inspect

    try:
        params = [
            p.name
            for p in inspect.signature(fn).parameters.values()
            if p.kind in (p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY)
        ]
    except (TypeError, ValueError):
        params = list(names)
    call_args = {k: locals_map[k] for k in params if k in locals_map}
    return lambda: fn(**call_args)


def verify_patch(flight, patched_source: str, expected_exc: str) -> Verification:
    from ._nondet import replay_tape

    invoke = _build_invocable_from_source(flight, patched_source)
    if invoke is None:
        return Verification(REJECTED, "recorded crash", "could not build patched invocable")

    tape = flight.tape() if flight.has_nondet else None

    def run():
        if tape is None or len(tape) == 0:
            return invoke()
        return replay_tape(tape, invoke)

    try:
        result = run()
    except ReplayDivergence:
        return Verification(CHANGES_BEHAVIOR, f"raised {expected_exc}", "diverged from the recorded tape")
    except BaseException as e:
        if type(e).__name__ == expected_exc:
            return Verification(REJECTED, f"raised {expected_exc}", f"still raises {expected_exc}")
        return Verification(
            CHANGES_BEHAVIOR, f"raised {expected_exc}", f"now raises {type(e).__name__}: {e}"
        )
    return Verification(VERIFIED, f"raised {expected_exc}", f"returns {result!r} (no divergence)")


@dataclass
class FixResult:
    status: str
    patch: Optional[str] = None
    tries: int = 0
    baseline: str = ""
    counterfactual: str = ""
    notes: list[str] = field(default_factory=list)

    @property
    def verified(self) -> bool:
        return self.status == VERIFIED

    def report(self) -> str:
        if self.status == NO_PATCH:
            return "no patch proposed (" + (self.notes[0] if self.notes else "unhandled crash shape") + ")"
        lines = [f"proposed patch ({self.tries} attempt{'s' if self.tries != 1 else ''}):"]
        if self.patch:
            lines.append("")
            lines.extend("  " + ln for ln in self.patch.rstrip("\n").splitlines())
            lines.append("")
        lines.append("verification over the recorded tape:")
        mark = {"VERIFIED": "✓", "CHANGES_BEHAVIOR": "⚠", "REJECTED": "✗"}.get(self.status, "?")
        if self.status == VERIFIED:
            lines.append("  ✓ the crash no longer reproduces")
            lines.append("  ✓ no boundary divergence (time/random/IO identical)")
            lines.append("  ⇒ FIX VERIFIED")
        elif self.status == CHANGES_BEHAVIOR:
            lines.append(f"  {mark} crash gone, but: {self.counterfactual}")
            lines.append("  ⇒ CHANGES BEHAVIOR — review before applying")
        else:
            lines.append(f"  {mark} {self.counterfactual}")
            lines.append("  ⇒ REJECTED")
        return "\n".join(lines)


def _default_provider() -> Optional[Callable]:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    try:
        import anthropic
    except ImportError:
        return None
    return _anthropic_provider


def _anthropic_provider(tools: AgentTools, feedback: str = "") -> Optional[str]:
    import anthropic

    frame = tools.crash_frame
    suspect = _find_suspect(tools.crash, frame) if frame else None
    slice_text = tools.why(0, suspect) if suspect else "(no clear suspect)"
    source = tools.source() or ""
    prompt = (
        "You are fixing a Python crash from its flight recorder black box.\n"
        f"Exception: {tools.exc_type}\n"
        f"Crash frame: {frame.qualname} ({os.path.basename(frame.file)}:{frame.lineno})\n\n"
        f"Backward slice of the suspect value:\n{slice_text}\n\n"
        f"Source of the crash file:\n{source}\n\n"
        + (f"Your previous attempt failed: {feedback}\n\n" if feedback else "")
        + "Return ONLY a unified diff (--- / +++ / @@) that fixes the crash "
        "without changing behaviour on the recorded inputs. No prose."
    )
    model = os.environ.get("FLIGHT_FIX_MODEL", "claude-sonnet-5")
    client = anthropic.Anthropic()
    msg = client.messages.create(
        model=model, max_tokens=2048, messages=[{"role": "user", "content": prompt}]
    )
    text = "".join(getattr(b, "text", "") for b in msg.content)
    if "```" in text:
        parts = text.split("```")
        text = max(parts, key=len)
        if text.startswith("diff") or text.startswith("patch"):
            text = text.split("\n", 1)[1] if "\n" in text else text
    return text.strip() + "\n" if text.strip() else None


def fix(flight_path, provider: Optional[Callable] = None, *, max_tries: int = 3, use_llm: bool = False) -> FixResult:
    tools = AgentTools(flight_path)
    if tools.crash_frame is None:
        return FixResult(NO_PATCH, notes=["no crash frames in this recording"])
    source = tools.source()
    if not source:
        return FixResult(NO_PATCH, notes=["source not captured for the crash frame"])

    resolved = provider or (_default_provider() if use_llm else None) or heuristic_patch
    expected = tools.exc_type
    feedback = ""
    last = FixResult(NO_PATCH, notes=["provider proposed no patch"])
    for attempt in range(1, max_tries + 1):
        try:
            diff = resolved(tools, feedback)
        except Exception as e:
            return FixResult(REJECTED, tries=attempt, counterfactual=f"provider error: {type(e).__name__}: {e}")
        if not diff:
            return FixResult(NO_PATCH, tries=attempt, notes=["provider proposed no patch"])
        patched = apply_unified_diff(source, diff)
        if patched is None:
            feedback = "the diff did not apply cleanly to the source"
            last = FixResult(REJECTED, patch=diff, tries=attempt, counterfactual=feedback)
            continue
        v = verify_patch(tools.flight, patched, expected)
        result = FixResult(
            v.status, patch=diff, tries=attempt, baseline=v.baseline, counterfactual=v.counterfactual
        )
        if v.status in (VERIFIED, CHANGES_BEHAVIOR):
            return result
        feedback = v.counterfactual
        last = result
    return last
