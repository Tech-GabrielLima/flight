"""Phase 7 — the intelligence layer: `flight explain`.

A `.flight` is the perfect structured context for an LLM — the exception chain,
the crash frame's locals, the object graph, the recent execution path and the
source, all already queryable. This module turns a crash into (1) a deterministic
**heuristic root-cause summary** you get offline, with no model and no network,
and (2) an **LLM-ready prompt** that bundles that context, which an optional
pluggable provider can turn into a natural-language explanation + suggested patch.

The valuable, testable core is the context builder and the heuristics; the model
call is a thin, injectable layer (`provider(prompt) -> text`), so `flight explain`
is useful and fully tested with no API key, and *becomes* an LLM explainer when
you configure one. Nothing here can crash the tool — a hostile recording yields a
degraded summary, never an exception (P1).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Callable, Optional

_EMPTYABLE = {"list", "dict", "tuple", "set", "frozenset", "str", "bytes"}


@dataclass
class Explanation:
    summary: str  # heuristic root cause (offline, deterministic)
    prompt: str  # an LLM-ready prompt bundling the crash context
    suspects: list[str] = field(default_factory=list)
    llm: Optional[str] = None  # a provider's explanation, if one ran

    def render(self) -> str:
        out = [self.summary]
        if self.llm:
            out.append("\n--- model explanation ---\n" + self.llm)
        return "\n".join(out)


# -- context ----------------------------------------------------------------


def _source_window(source: str, lineno: int, radius: int = 3) -> list[str]:
    lines = source.splitlines()
    lo = max(1, lineno - radius)
    hi = min(len(lines), lineno + radius)
    out = []
    for n in range(lo, hi + 1):
        mark = "→" if n == lineno else " "
        out.append(f"{mark} {n:>4} {lines[n - 1]}")
    return out


def _suspicion(node: Optional[dict]) -> Optional[str]:
    """Why a value looks like the culprit — or None if it looks fine."""
    if node is None:
        return None
    kind = node.get("kind")
    if kind == "none":
        return "is None"
    if kind == "int" and node.get("repr") == "0":
        return "is zero"
    if kind in _EMPTYABLE and node.get("length") == 0:
        return "is empty"
    return None


def build_context(flight_path) -> dict:
    """Assemble the structured crash context both the heuristics and the LLM
    prompt draw from. Never raises: an unreadable crash yields ``{}``-ish."""
    from ._read import read

    fl = read(flight_path)
    ctx: dict = {"has_crash": fl.has_crash, "exceptions": [], "frames": [], "suspects": []}
    if not fl.has_crash:
        ctx["events"] = fl.events(limit=20)
        return ctx
    crash = fl.crash()
    ctx["exceptions"] = list(crash.exceptions)
    if not crash.frames:
        return ctx
    top = crash.frames[0]
    ctx["crash_frame"] = {"qualname": top.qualname, "file": top.file, "line": top.lineno}
    src = crash.sources.get(top.file)
    ctx["source_window"] = _source_window(src, top.lineno) if src else []
    # crash-frame locals, rendered, with suspicion + aliasing flags
    locs = []
    for name, oid in top.locals:
        if name.startswith("__") and name.endswith("__"):
            continue
        rendered = crash.render(oid)
        why = _suspicion(crash.node(oid))
        aliased = len(crash.aliases(oid)) > 1
        locs.append({"name": name, "value": rendered, "why": why, "aliased": aliased})
        if why:
            ctx["suspects"].append(f"{name} ({rendered}) {why}")
    ctx["locals"] = locs
    # the outer call path (qualname per frame), crash-first
    ctx["stack"] = [f"{fr.qualname} ({os.path.basename(fr.file)}:{fr.lineno})" for fr in crash.frames]
    return ctx


# -- heuristic analysis (offline) ------------------------------------------


def analyze(ctx: dict) -> tuple[str, list[str]]:
    """A deterministic root-cause summary from the context. Returns
    ``(summary, suspects)``."""
    if not ctx.get("has_crash"):
        return ("This recording has no crash (no exception/frames captured).", [])
    excs = ctx.get("exceptions") or []
    if not excs:
        return ("A crash was recorded but its exception was not captured.", [])
    etype, emsg, _rel = excs[0]
    cf = ctx.get("crash_frame")
    where = f"{cf['qualname']} ({os.path.basename(cf['file'])}:{cf['line']})" if cf else "?"
    lines = [f"{etype}: {emsg}".rstrip(": ") + f"\n  crashed in {where}"]

    suspects = ctx.get("suspects") or []
    if suspects:
        lines.append("  likely cause — suspicious state at the crash:")
        for s in suspects:
            lines.append(f"    • {s}")
        # A pointed guess for the classic cases.
        first = suspects[0]
        if etype == "ZeroDivisionError":
            lines.append("  → a divisor is zero.")
        elif etype in ("IndexError", "KeyError", "StopIteration"):
            lines.append("  → an empty/short container was indexed or iterated.")
        elif "is None" in first and etype == "AttributeError":
            lines.append("  → an attribute was accessed on None.")
    aliased = [loc["name"] for loc in ctx.get("locals", []) if loc.get("aliased")]
    if aliased:
        lines.append(f"  note: {', '.join(aliased)} is the SAME object across frames (aliased).")
    if ctx.get("exceptions") and len(excs) > 1:
        chain = " ← ".join(e[0] for e in excs)
        lines.append(f"  exception chain: {chain}")
    return ("\n".join(lines), suspects)


# -- LLM prompt -------------------------------------------------------------


def prompt_text(ctx: dict) -> str:
    """A compact, model-agnostic prompt bundling the crash context."""
    if not ctx.get("has_crash"):
        return "No crash was recorded in this .flight; nothing to explain."
    excs = ctx.get("exceptions") or []
    lines = [
        "You are debugging a Python program from its flight recorder black box.",
        "Explain the ROOT CAUSE in 2-3 sentences, then suggest a concrete patch.",
        "",
        "Exception chain (most recent first):",
    ]
    for etype, emsg, rel in excs:
        tag = f" [{rel}]" if rel and rel != "root" else ""
        lines.append(f"  {etype}: {emsg}{tag}")
    if ctx.get("stack"):
        lines.append("")
        lines.append("Stack (crash first):")
        lines.extend(f"  {s}" for s in ctx["stack"])
    if ctx.get("source_window"):
        lines.append("")
        lines.append("Source at the crash:")
        lines.extend(f"  {ln}" for ln in ctx["source_window"])
    if ctx.get("locals"):
        lines.append("")
        lines.append("Locals in the crash frame:")
        for loc in ctx["locals"]:
            flag = f"   <-- {loc['why']}" if loc["why"] else ""
            alias = " (aliased)" if loc["aliased"] else ""
            lines.append(f"  {loc['name']} = {loc['value']}{alias}{flag}")
    return "\n".join(lines)


# -- provider (optional) ----------------------------------------------------


def _default_provider() -> Optional[Callable[[str], str]]:
    """An Anthropic-backed provider if `anthropic` + an API key are present;
    otherwise None (so `explain` stays offline by default)."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    try:
        import anthropic
    except ImportError:
        return None
    model = os.environ.get("FLIGHT_EXPLAIN_MODEL", "claude-sonnet-5")

    def provider(prompt: str) -> str:
        client = anthropic.Anthropic()
        msg = client.messages.create(
            model=model, max_tokens=1024, messages=[{"role": "user", "content": prompt}]
        )
        return "".join(getattr(b, "text", "") for b in msg.content)

    return provider


def explain(flight_path, provider: Optional[Callable[[str], str]] = None, *, use_llm: bool = False):
    """Explain a crash `.flight`. Deterministic by default (heuristics + an
    LLM-ready prompt); if `provider` is given (or `use_llm` and one is
    configured), also returns the model's explanation. See :class:`Explanation`."""
    ctx = build_context(flight_path)
    summary, suspects = analyze(ctx)
    prompt = prompt_text(ctx)
    llm = None
    resolved = provider or (_default_provider() if use_llm else None)
    if resolved is not None:
        try:
            llm = resolved(prompt)
        except Exception as e:  # a model/network failure never breaks explain (P1)
            llm = f"(model call failed: {type(e).__name__}: {e})"
    return Explanation(summary=summary, prompt=prompt, suspects=suspects, llm=llm)
