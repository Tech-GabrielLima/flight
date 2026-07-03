"""What-if debugging — the moonshot (Phase 10, VISION.md §5.6).

Every earlier phase builds toward a question you normally can't ask of a crash:
*what if this value had been different?* Because Flight can hold the whole
recorded world constant (the Phase-3 deterministic tape — time, random, uuid,
I/O, the schedule) and reconstruct state at any point, it can **re-execute the
run with one value changed and show you the counterfactual outcome**: "what if
`numbers` weren't empty here?" — the program keeps going and you see where it
ends up, with everything *else* exactly as it was recorded.

The mechanism is two faithful replays of the same function over the same tape:

* the **baseline** replay reproduces the recorded outcome (bit-for-bit);
* the **counterfactual** replay runs with a trace hook that, the moment control
  reaches a chosen line, overwrites a local variable with your value.

Overwriting a live local is possible without any bytecode surgery on Python
3.13+, where ``frame.f_locals`` is a write-through proxy (PEP 667): assigning to
it from a trace callback updates the real fast local the next bytecode reads.
On older Pythons the override can't take effect and `what_if` says so rather
than lying.

Three honest outcomes fall out of the counterfactual replay:

* it **returns** (or raises) something different — the counterfactual result;
* it **diverges** from the tape — the change would take a different path through
  the recorded world (e.g. it now calls ``random()`` one more time), which is
  itself a finding: the edit isn't consistent with the recording;
* it **doesn't reach** the override point — reported, not silently ignored.

Everything obeys P1: a failure in the machinery never escapes as a surprise; the
counterfactual's *own* exception is captured and reported, not raised at you.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from typing import Any, Optional

from ._nondet import ReplayDivergence

_PEP667 = sys.version_info >= (3, 13)


def _safe_repr(value: Any, limit: int = 200) -> str:
    try:
        r = repr(value)
    except BaseException as e:  # a hostile __repr__
        return f"<repr failed: {type(e).__name__}>"
    return r if len(r) <= limit else r[:limit] + "…"


@dataclass
class Override:
    """Change local ``var`` to ``value`` the ``nth`` time control reaches
    ``line`` (optionally only inside function ``qualname``).

    The override is applied *just before* ``line`` runs, so target the line that
    **uses** the value, not the line that assigns it. Requires Python 3.13+
    (PEP 667 write-through locals)."""

    var: str
    value: Any
    line: int
    qualname: Optional[str] = None
    nth: int = 1
    #: Set after a counterfactual run: whether the override actually fired…
    applied: bool = field(default=False, compare=False)
    #: …and the value it replaced (repr), for the human diff.
    previous: Optional[str] = field(default=None, compare=False)

    def describe(self) -> str:
        where = f"{self.qualname}:{self.line}" if self.qualname else f"line {self.line}"
        was = f" (was {self.previous})" if self.previous is not None else ""
        return f"{self.var} := {_safe_repr(self.value)} at {where}{was}"


@dataclass
class Outcome:
    """How a run ended: a value, an exception, or a divergence from the tape."""

    returned: Any = None
    exception: Optional[BaseException] = None
    diverged: bool = False

    @property
    def raised(self) -> bool:
        return self.exception is not None and not self.diverged

    def key(self):
        """A comparable summary of the outcome (for `WhatIf.changed`)."""
        if self.diverged:
            return ("diverged",)
        if self.exception is not None:
            return ("raised", type(self.exception).__name__, str(self.exception))
        return ("returned", _safe_repr(self.returned))

    def describe(self) -> str:
        if self.diverged:
            return "diverged from the recorded run (a different path through the recorded world)"
        if self.exception is not None:
            return f"raised {type(self.exception).__name__}: {self.exception}"
        return f"returned {_safe_repr(self.returned)}"


@dataclass
class WhatIf:
    """The result of a what-if: the recorded outcome vs the counterfactual."""

    baseline: Outcome
    counterfactual: Outcome
    overrides: list[Override]

    @property
    def changed(self) -> bool:
        """True if changing the value changed how the run ended."""
        return self.baseline.key() != self.counterfactual.key()

    @property
    def unreached(self) -> list[Override]:
        """Overrides whose line was never hit in the counterfactual run."""
        return [o for o in self.overrides if not o.applied]

    def render(self) -> str:
        lines = ["what-if:"]
        for o in self.overrides:
            miss = "" if o.applied else "   ⚠ never reached"
            lines.append(f"  · {o.describe()}{miss}")
        lines.append(f"  before: {self.baseline.describe()}")
        lines.append(f"  after:  {self.counterfactual.describe()}")
        if not _PEP667:
            lines.append("  (note: live-local override needs Python 3.13+ — outcome unchanged here)")
        elif self.changed:
            lines.append("  → the change alters the outcome.")
        else:
            lines.append("  → no change to the outcome.")
        return "\n".join(lines)


def _make_tracer(overrides: list[Override]):
    counts: dict[int, int] = {}

    def tracer(frame, event, _arg):
        if event == "call":
            return tracer
        if event == "line":
            code = frame.f_code
            for ov in overrides:
                if ov.qualname is not None and code.co_qualname != ov.qualname:
                    continue
                if frame.f_lineno != ov.line:
                    continue
                key = id(ov)
                counts[key] = counts.get(key, 0) + 1
                if counts[key] == ov.nth:
                    try:
                        ov.previous = _safe_repr(frame.f_locals.get(ov.var, "<undefined>"))
                        frame.f_locals[ov.var] = ov.value  # PEP 667 write-through (3.13+)
                        ov.applied = True
                    except Exception:
                        pass
        return tracer

    return tracer


def _run(tape, fn, args, kwargs, tracer) -> Outcome:
    from ._nondet import replay_tape

    target = fn
    if tracer is not None:

        def traced(*a, **k):
            old = sys.gettrace()
            sys.settrace(tracer)
            try:
                return fn(*a, **k)
            finally:
                sys.settrace(old)

        target = traced

    try:
        result = replay_tape(tape, target, *args, **kwargs)
        return Outcome(returned=result)
    except ReplayDivergence:
        return Outcome(diverged=True)
    except BaseException as e:  # the run's own outcome, captured not raised (P1)
        return Outcome(exception=e)


def what_if(flight_path, fn, overrides, *args, **kwargs) -> WhatIf:
    """Re-execute `fn` over the deterministic tape in `flight_path`, once as
    recorded (the baseline) and once with `overrides` applied (the
    counterfactual), and return both outcomes.

    `overrides` is an :class:`Override` or a list of them. Extra ``*args`` /
    ``**kwargs`` are passed to `fn`, exactly as :func:`flight.replay`.

        # what if `numbers` weren't empty at line 42?
        wi = flight.what_if("run.flight", compute,
                            flight.Override("numbers", [1, 2, 3], line=42))
        print(wi.render())
    """
    from ._read import read

    if isinstance(overrides, Override):
        overrides = [overrides]
    overrides = list(overrides)

    # A fresh tape per run: replay advances the tape's cursors, so the two runs
    # must not share one.
    baseline = _run(read(flight_path).tape(), fn, args, kwargs, tracer=None)
    counterfactual = _run(read(flight_path).tape(), fn, args, kwargs, tracer=_make_tracer(overrides))
    return WhatIf(baseline=baseline, counterfactual=counterfactual, overrides=overrides)
