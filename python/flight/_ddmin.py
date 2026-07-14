from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional

_NEUTRAL = {"i": "0", "f": "0.0", "o": "0", "s": "", "b": ""}


def ddmin(items: list, test: Callable[[list], bool]) -> list:
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

    reproduced: bool
    total: int
    kept: list[int]
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
    from ._read import read

    return minimize_tape(read(flight_path).tape(), fn, interesting, *args, **kwargs)
