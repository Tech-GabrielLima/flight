"""Phase 6 — debugging by comparison: `flight diff` and delta debugging."""

from __future__ import annotations

import random
import shutil
import subprocess
import sys

import flight
from flight import Mutation, Recording
from flight._ddmin import ddmin, minimize_tape
from flight._diff import diff_events, diff_mutations, diff_tapes
from flight._nondet import Tape


# -- helpers ----------------------------------------------------------------


def _mut(seq, name, repr_):
    return Mutation(
        seq=seq, kind="local", name=name, key=None,
        value=("int", repr_, None, None), file="t.py", qualname="f", line=10, frame=1,
    )


def _record_tape(path, seed, n=5):
    def work():
        random.seed(seed)
        return [random.randint(1, 6) for _ in range(n)]

    with flight.deterministic(str(path)):
        work()
    return str(path)


# -- diff: mutation timelines (pure, precise alignment) --------------------


def test_identical_mutations_report_identical():
    a = Recording([_mut(0, "a", "1"), _mut(1, "b", "2"), _mut(2, "c", "3")])
    b = Recording([_mut(0, "a", "1"), _mut(1, "b", "2"), _mut(2, "c", "3")])
    d = diff_mutations(a, b)
    assert d.kind == "mutation" and d.identical and not d


def test_mutations_diverge_at_the_first_differing_value():
    a = Recording([_mut(0, "a", "1"), _mut(1, "b", "2"), _mut(2, "c", "3")])
    b = Recording([_mut(0, "a", "1"), _mut(1, "b", "2"), _mut(2, "c", "9")])
    d = diff_mutations(a, b)
    assert not d.identical
    assert d.index == 2
    assert "c" in d.detail
    assert d.left != d.right


def test_mutations_diverge_when_one_kept_writing():
    a = Recording([_mut(0, "a", "1"), _mut(1, "b", "2")])
    b = Recording([_mut(0, "a", "1")])
    d = diff_mutations(a, b)
    assert not d.identical and d.index == 1
    assert "left" in d.detail  # left recording kept writing


def test_diff_render_is_readable():
    a = Recording([_mut(0, "a", "1"), _mut(1, "b", "2")])
    b = Recording([_mut(0, "a", "1"), _mut(1, "b", "5")])
    text = diff_mutations(a, b).render()
    assert "diverged at step" in text
    assert "left" in text and "right" in text


# -- diff: non-determinism tapes -------------------------------------------


def test_identical_tapes(tmp_path):
    a = _record_tape(tmp_path / "a.flight", seed=1)
    b = _record_tape(tmp_path / "b.flight", seed=1)
    d = flight.diff(a, b)
    assert d.kind == "nondet"
    assert d.identical


def test_tapes_diverge_on_a_different_answer(tmp_path):
    a = _record_tape(tmp_path / "a.flight", seed=1)
    b = _record_tape(tmp_path / "b.flight", seed=2)
    d = flight.diff(a, b)
    assert d.kind == "nondet"
    assert not d.identical
    assert "random" in d.detail


def test_diff_tapes_detects_control_flow_branch():
    # same first call, then different sources -> control flow branched
    a = Tape([(0, "time.time", "f", "1.0"), (1, "random.random", "f", "0.5")])
    b = Tape([(0, "time.time", "f", "1.0"), (1, "os.urandom", "b", "00")])
    d = diff_tapes(a, b)
    assert not d.identical
    assert d.index == 1
    assert "branched" in d.detail


# -- diff: events & edge cases ---------------------------------------------


def test_diff_events_pure():
    a = [("PY_START", "x.py", "f", 1), ("LINE", "x.py", "f", 2)]
    b = [("PY_START", "x.py", "f", 1), ("LINE", "x.py", "f", 3)]
    d = diff_events(a, b)
    assert d.kind == "event" and d.index == 1 and not d.identical


def test_diff_a_file_against_itself_is_identical(tmp_path):
    a = _record_tape(tmp_path / "a.flight", seed=7)
    assert flight.diff(a, a).identical


# -- CLI --------------------------------------------------------------------


def test_cli_diff_exit_codes(tmp_path):
    a = _record_tape(tmp_path / "a.flight", seed=1)
    b = _record_tape(tmp_path / "b.flight", seed=2)
    same_copy = str(tmp_path / "a_copy.flight")
    shutil.copy(a, same_copy)  # identical content, different filename
    same = subprocess.run(
        [sys.executable, "-m", "flight", "diff", a, same_copy], capture_output=True, text=True
    )
    assert same.returncode == 0 and "identical" in same.stdout
    diff = subprocess.run(
        [sys.executable, "-m", "flight", "diff", a, b], capture_output=True, text=True
    )
    assert diff.returncode == 1 and "diverged" in diff.stdout


# -- delta debugging: pure ddmin -------------------------------------------


def test_ddmin_finds_the_minimal_culprit_pair():
    items = list(range(10))
    # "interesting" iff both 3 and 7 are present
    result = ddmin(items, lambda s: 3 in s and 7 in s)
    assert set(result) == {3, 7}


def test_ddmin_single_culprit():
    items = list(range(8))
    assert ddmin(items, lambda s: 5 in s) == [5]


# -- delta debugging: minimize a tape --------------------------------------


def _crashy():
    # crashes iff the 4th recorded draw exceeds 90 — the only value that matters
    vals = [random.randint(0, 100) for _ in range(6)]
    if vals[3] > 90:
        raise ValueError("boom")
    return vals


def test_minimize_isolates_the_one_value_that_matters():
    # A hand-built tape: six random.randint draws, the 4th is the culprit.
    draws = [10, 20, 30, 95, 40, 50]
    rows = [(i, "random.randint", "i", str(v)) for i, v in enumerate(draws)]
    res = minimize_tape(Tape(rows), _crashy)
    assert res.reproduced
    assert res.total == 6
    assert res.kept == [3]  # only the 4th draw is load-bearing
    assert res.neutralized == 5
    assert res.kept_rows[0][3] == "95"


def test_minimize_reports_when_it_never_reproduced():
    draws = [1, 2, 3, 4, 5, 6]  # 4th is 4, never > 90 -> no crash
    rows = [(i, "random.randint", "i", str(v)) for i, v in enumerate(draws)]
    res = minimize_tape(Tape(rows), _crashy)
    assert res.reproduced is False


def test_minimize_render():
    rows = [(i, "random.randint", "i", str(v)) for i, v in enumerate([1, 2, 3, 95, 5, 6])]
    text = minimize_tape(Tape(rows), _crashy).render()
    assert "minimal reproducer" in text and "95" in text
