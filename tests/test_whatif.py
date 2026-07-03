"""Phase 10 — what-if debugging (the moonshot).

Re-execute a recorded run over its deterministic tape with one value changed,
and see the counterfactual outcome — with the recorded world (here, `random`)
held constant. These tests exercise the four outcomes: the change fixes the
crash, the change is inert, the override never fires, and the change diverges
from the recorded tape.
"""

from __future__ import annotations

import inspect
import random
import sys

import pytest

import flight
from flight import Override

pytestmark = pytest.mark.skipif(
    sys.version_info < (3, 13), reason="what-if needs PEP 667 write-through locals (3.13+)"
)


def _line(fn, marker: str) -> int:
    src, start = inspect.getsourcelines(fn)
    for i, line in enumerate(src):
        if marker in line:
            return start + i
    raise AssertionError(f"marker {marker!r} not found in {fn.__name__}")


# --- functions under test (module level so replay can resolve them) --------


def crashing():
    data = []
    factor = random.randint(100, 999)            # recorded on the tape
    return factor * (sum(data) / len(data))      # WHATIF_USE (ZeroDivision when empty)


def branch():
    flag = False
    if flag:                                     # WHATIF_FLAG
        random.random()                          # only called if flag is True
    return random.randint(0, 9)                  # recorded on the tape


def pure():
    x = 5
    return x * 2                                 # WHATIF_PURE (no non-determinism)


def _record(path, fn):
    """Record a deterministic run of `fn` (swallowing any crash) → a tape."""
    try:
        with flight.deterministic(str(path)):
            fn()
    except Exception:
        pass
    return str(path)


# --- the four outcomes -----------------------------------------------------


def test_change_turns_a_crash_into_a_result(tmp_path):
    path = _record(tmp_path / "c.flight", crashing)
    wi = flight.what_if(path, crashing, Override("data", [2, 4], line=_line(crashing, "WHATIF_USE")))

    assert wi.baseline.raised and isinstance(wi.baseline.exception, ZeroDivisionError)
    assert not wi.counterfactual.raised
    assert wi.changed
    ov = wi.overrides[0]
    assert ov.applied
    assert ov.previous == "[]"
    # the counterfactual returned recorded_factor * (6/2); factor is a multiple of 1,
    # so the result is a multiple of 3 — and > 0 (a real number, not a crash).
    assert wi.counterfactual.returned == pytest.approx(round(wi.counterfactual.returned))
    assert wi.counterfactual.returned % 3 == 0


def test_counterfactual_holds_the_recorded_world_constant(tmp_path):
    """Two what-ifs give the same counterfactual: `random` came from the tape."""
    path = _record(tmp_path / "c.flight", crashing)
    line = _line(crashing, "WHATIF_USE")
    a = flight.what_if(path, crashing, Override("data", [1, 1], line=line))
    b = flight.what_if(path, crashing, Override("data", [1, 1], line=line))
    assert a.counterfactual.returned == b.counterfactual.returned
    assert not a.counterfactual.raised


def test_inert_change_does_not_alter_the_outcome(tmp_path):
    path = _record(tmp_path / "c.flight", crashing)
    # still empty → still divides by zero
    wi = flight.what_if(path, crashing, Override("data", [], line=_line(crashing, "WHATIF_USE")))
    assert wi.overrides[0].applied  # the override *did* fire
    assert wi.counterfactual.raised and isinstance(wi.counterfactual.exception, ZeroDivisionError)
    assert not wi.changed


def test_override_that_is_never_reached_is_reported(tmp_path):
    path = _record(tmp_path / "c.flight", crashing)
    wi = flight.what_if(path, crashing, Override("data", [9], line=999999))
    assert not wi.overrides[0].applied
    assert wi.unreached == wi.overrides
    assert not wi.changed  # outcome identical to baseline (still crashes)
    assert wi.counterfactual.raised


def test_change_that_diverges_from_the_tape(tmp_path):
    """Flipping the branch makes the code call `random()` a time the tape never
    recorded — a divergence, which is itself the finding."""
    path = _record(tmp_path / "b.flight", branch)
    wi = flight.what_if(path, branch, Override("flag", True, line=_line(branch, "WHATIF_FLAG")))
    assert not wi.baseline.diverged  # the recorded path replays cleanly
    assert wi.counterfactual.diverged
    assert wi.changed
    assert "diverged" in wi.counterfactual.describe()


def test_what_if_on_a_deterministic_function(tmp_path):
    path = _record(tmp_path / "p.flight", pure)
    wi = flight.what_if(path, pure, Override("x", 10, line=_line(pure, "WHATIF_PURE")))
    assert wi.baseline.returned == 10
    assert wi.counterfactual.returned == 20
    assert wi.changed and wi.overrides[0].previous == "5"


# --- rendering / API -------------------------------------------------------


def test_render_reads_like_a_report(tmp_path):
    path = _record(tmp_path / "c.flight", crashing)
    wi = flight.what_if(path, crashing, Override("data", [2, 4], line=_line(crashing, "WHATIF_USE")))
    text = wi.render()
    assert "what-if:" in text
    assert "before:" in text and "after:" in text
    assert "alters the outcome" in text


def test_single_override_or_list_both_accepted(tmp_path):
    path = _record(tmp_path / "c.flight", crashing)
    line = _line(crashing, "WHATIF_USE")
    one = flight.what_if(path, crashing, Override("data", [3], line=line))
    many = flight.what_if(path, crashing, [Override("data", [3], line=line)])
    assert one.counterfactual.returned == many.counterfactual.returned
