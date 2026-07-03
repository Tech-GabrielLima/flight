"""Phase 6 — debugging by comparison: `flight diff`.

Two recordings of the "same" program — one that worked, one that failed — carry
the answer to *why* between them: the **first point they diverged**. A traceback
can't show that; a diff of the timelines can. flight compares two `.flight`
files position by position and reports the earliest step where they differ:

- **mutation timelines** (scope recordings): the first state write whose target
  or value differs — "at step 12, `total` was 40 here but 39 there";
- **non-determinism tapes** (deterministic runs): the first boundary call that
  answered differently — "the 7th `random()` returned 0.83 vs 0.11", or a
  *source* mismatch meaning control flow branched — the root of a flaky test;
- **event rings** otherwise: the first execution step (call/line/return) that
  took a different path.

Position-by-position is the right model: two runs of the same code march in
lockstep until the run diverges, so the first mismatch *is* the cause boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class Divergence:
    """Where (and how) two recordings first differ."""

    kind: str  # "mutation" | "nondet" | "event" | "incomparable"
    identical: bool
    index: Optional[int]  # position of the first difference (0-based)
    left: Optional[str]  # rendering of the left recording at that point
    right: Optional[str]  # rendering of the right recording at that point
    detail: str  # a one-line human explanation
    compared: int = 0  # how many positions were compared

    def __bool__(self) -> bool:
        """Truthy when the recordings diverged."""
        return not self.identical

    def render(self) -> str:
        head = f"comparing {self.kind}s"
        if self.identical:
            return f"{head}: identical ({self.compared} steps compared)"
        lines = [
            f"{head}: diverged at step {self.index} ({self.compared} steps compared)",
            f"  {self.detail}",
        ]
        if self.left is not None or self.right is not None:
            lines.append(f"  left : {self.left}")
            lines.append(f"  right: {self.right}")
        return "\n".join(lines)


def _identical(kind: str, compared: int) -> Divergence:
    return Divergence(kind, True, None, None, None, "the recordings match", compared)


# -- mutation timelines -----------------------------------------------------


def _mut_key(m) -> tuple:
    """The identity of a write for alignment: where + what it targeted."""
    return (m.kind, m.name, m.key, m.line)


def diff_mutations(a, b) -> Divergence:
    """First mutation where two scope `Recording`s differ (target or value)."""
    ma, mb = a.mutations, b.mutations
    n = min(len(ma), len(mb))
    for i in range(n):
        x, y = ma[i], mb[i]
        if _mut_key(x) != _mut_key(y):
            return Divergence(
                "mutation", False, i, _render_mut(x), _render_mut(y),
                f"different write here ({_target(x)} vs {_target(y)})", n,
            )
        if x.value_repr != y.value_repr:
            return Divergence(
                "mutation", False, i, _render_mut(x), _render_mut(y),
                f"{_target(x)} = {x.value_repr!r} here but {y.value_repr!r} there", n,
            )
    if len(ma) != len(mb):
        longer = "left" if len(ma) > len(mb) else "right"
        extra = (ma if longer == "left" else mb)[n]
        return Divergence(
            "mutation", False, n,
            _render_mut(ma[n]) if len(ma) > n else None,
            _render_mut(mb[n]) if len(mb) > n else None,
            f"{longer} recording kept writing ({_render_mut(extra)})", n,
        )
    return _identical("mutation", n)


def _target(m) -> str:
    if m.kind == "local":
        return m.name
    return f"{m.name}[{m.key}]" if m.kind == "item" else f"{m.name}.{m.key}"


def _render_mut(m) -> str:
    return f"#{m.seq} {m.kind} {_target(m)} = {m.value_repr}"


# -- non-determinism tapes --------------------------------------------------


def diff_tapes(a, b) -> Divergence:
    """First boundary call where two deterministic `Tape`s answered differently.
    A `source` mismatch means the code took a different branch (control flow
    diverged); a `payload` mismatch means the world answered differently."""
    ra, rb = a.rows(), b.rows()
    n = min(len(ra), len(rb))
    for i in range(n):
        _sa, srca, taga, pa = ra[i]
        _sb, srcb, tagb, pb = rb[i]
        if srca != srcb:
            return Divergence(
                "nondet", False, i, srca, srcb,
                f"control flow branched: {srca} vs {srcb}", n,
            )
        if (taga, pa) != (tagb, pb):
            return Divergence(
                "nondet", False, i, _render_row(ra[i]), _render_row(rb[i]),
                f"{srca} answered differently", n,
            )
    if len(ra) != len(rb):
        longer = "left" if len(ra) > len(rb) else "right"
        return Divergence(
            "nondet", False, n,
            _render_row(ra[n]) if len(ra) > n else None,
            _render_row(rb[n]) if len(rb) > n else None,
            f"{longer} recording made more boundary calls", n,
        )
    return _identical("nondet", n)


def _render_row(row) -> str:
    _seq, src, tag, payload = row
    p = payload if len(payload) <= 40 else payload[:40] + "…"
    return f"{src} [{tag}] {p}"


# -- event rings ------------------------------------------------------------


def diff_events(a_events, b_events) -> Divergence:
    """First ring event (kind, file, qualname, line) that took a different path."""
    n = min(len(a_events), len(b_events))
    for i in range(n):
        if tuple(a_events[i]) != tuple(b_events[i]):
            return Divergence(
                "event", False, i, _render_event(a_events[i]), _render_event(b_events[i]),
                "execution path diverged here", n,
            )
    if len(a_events) != len(b_events):
        longer = "left" if len(a_events) > len(b_events) else "right"
        return Divergence("event", False, n, None, None, f"{longer} ran longer", n)
    return _identical("event", n)


def _render_event(e) -> str:
    kind, file, qual, line = e
    import os

    return f"{kind} {qual} ({os.path.basename(file)}:{line})"


# -- auto-detecting file diff ----------------------------------------------


def diff_files(path_a: str, path_b: str) -> Divergence:
    """Compare two `.flight` files, choosing the richest axis they share:
    mutation timeline, then non-determinism tape, then the event ring."""
    from ._read import read

    fa, fb = read(path_a), read(path_b)
    if fa.has_mutations and fb.has_mutations:
        return diff_mutations(fa.recording(), fb.recording())
    if fa.has_nondet and fb.has_nondet:
        return diff_tapes(fa.tape(), fb.tape())
    ea, eb = fa.events(), fb.events()
    if ea and eb:
        # Rings are stored most-recent-last; compare chronologically.
        return diff_events(ea, eb)
    return Divergence(
        "incomparable", False, None, None, None,
        "the two files share no comparable axis (mutations / tape / events)", 0,
    )
