"""Phase 6 — delta debugging: shrink a recording to its minimal reproducer.

A deterministic `.flight` can hold hundreds of recorded values, but usually only
a handful *cause* the failure. Delta debugging (Zeller's ddmin) finds them: it
repeatedly replays the run with more and more of the recorded values replaced by
a neutral default, keeping only the reductions that still reproduce the failure,
until every remaining original value is load-bearing — "your bug needs only
these 3 of the 500 recorded values."

The test at each step is a real **replay** (:func:`flight.replay_tape`) of the
same function under a partially-neutralized tape, checked by an `interesting`
predicate (default: it still raises). Neutralizing a value that changes control
flow makes the replay diverge — which the predicate reads as *not reproducing*,
so that value is correctly kept. The generic :func:`ddmin` is pure and unit
tested on its own; :func:`minimize_tape` wires it to the replay engine.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional

#: Neutral payload per scalar/IO tag — the "default value" a reduction tries.
_NEUTRAL = {"i": "0", "f": "0.0", "o": "0", "s": "", "b": ""}


def ddmin(items: list, test: Callable[[list], bool]) -> list:
    """Return a 1-minimal sublist of `items` for which `test` still holds.

    `test(subset)` must be monotone-ish and true for the full list. Classic
    Zeller ddmin: try removing ever-finer chunks; keep any removal that keeps
    `test` true; stop when no single chunk can be removed."""
    items = list(items)
    n = 2
    while len(items) >= 2:
        chunk = max(1, len(items) // n)
        subsets = [items[i : i + chunk] for i in range(0, len(items), chunk)]
        reduced = False
        for s in subsets:
            complement = [x for x in items if x not in s]
            if complement and test(complement):
                items = complement
                n = max(n - 1, 2)
                reduced = True
                break
        if not reduced:
            if n >= len(items):
                break
            n = min(len(items), 2 * n)
    return items


@dataclass
class MinimizeResult:
    """The outcome of minimizing a tape."""

    reproduced: bool  # did the *original* recording reproduce the failure at all?
    total: int  # neutralizable entries considered
    kept: list[int]  # indices whose original value is load-bearing
    kept_rows: list[tuple] = field(default_factory=list)
    neutralized: int = 0

    def render(self) -> str:
        if not self.reproduced:
            return "the recording did not reproduce the failure (nothing to minimize)"
        lines = [
            f"minimal reproducer: {len(self.kept)} of {self.total} recorded values matter "
            f"({self.neutralized} neutralized)"
        ]
        for seq, src, tag, payload in self.kept_rows:
            p = payload if len(payload) <= 48 else payload[:48] + "…"
            lines.append(f"  #{seq} {src} [{tag}] = {p}")
        return "\n".join(lines)


def _neutralize(row: tuple) -> tuple:
    seq, src, tag, _payload = row
    return (seq, src, tag, _NEUTRAL[tag])


def _run(rows: list, fn, args, kwargs) -> dict:
    from ._nondet import ReplayDivergence, Tape, replay_tape

    try:
        value = replay_tape(Tape(rows), fn, *args, **kwargs)
        return {"raised": False, "exc": None, "value": value, "diverged": False}
    except ReplayDivergence:
        # A neutralized value broke control flow — treat as "did not reproduce",
        # so the value that caused it stays in the minimal set.
        return {"raised": False, "exc": None, "value": None, "diverged": True}
    except BaseException as e:
        return {"raised": True, "exc": type(e).__name__, "value": None, "diverged": False}


def minimize_tape(
    tape,
    fn,
    interesting: Optional[Callable[[dict], bool]] = None,
    *args,
    **kwargs,
) -> MinimizeResult:
    """Shrink `tape` to the minimal set of original values needed for `fn` to
    stay *interesting* (default: still raise). Returns a :class:`MinimizeResult`.

    `interesting(outcome)` inspects ``{"raised", "exc", "value", "diverged"}``."""
    interesting = interesting or (lambda o: o["raised"])
    rows = tape.rows()
    neutralizable = [i for i, r in enumerate(rows) if r[2] in _NEUTRAL]
    neutral_set = set(neutralizable)

    if not interesting(_run(rows, fn, args, kwargs)):
        return MinimizeResult(reproduced=False, total=len(neutralizable), kept=list(neutralizable))

    def test(keep: list) -> bool:
        keepset = set(keep)
        trial = [
            _neutralize(r) if (i in neutral_set and i not in keepset) else r
            for i, r in enumerate(rows)
        ]
        return interesting(_run(trial, fn, args, kwargs))

    kept = ddmin(neutralizable, test)
    return MinimizeResult(
        reproduced=True,
        total=len(neutralizable),
        kept=kept,
        kept_rows=[rows[i] for i in kept],
        neutralized=len(neutralizable) - len(kept),
    )


def minimize(flight_path, fn, interesting=None, *args, **kwargs) -> MinimizeResult:
    """Delta-debug a deterministic `.flight`: the minimal set of its recorded
    values for which replaying `fn` still fails. See :func:`minimize_tape`."""
    from ._read import read

    return minimize_tape(read(flight_path).tape(), fn, interesting, *args, **kwargs)
