from __future__ import annotations

import random
import subprocess
import sys

import flight
from flight._generalize import Generalization, generalize, generalize_tape


def crashes_if_big():
    n = random.randint(0, 1000)
    if n >= 95:
        raise ValueError(f"too big: {n}")
    return n


def crashes_if_small():
    n = random.randint(0, 1000)
    if n <= 10:
        raise ValueError(f"too small: {n}")
    return n


def test_boundary_is_exact_ge(tmp_path):
    tape = flight.Tape([(0, "random.randint", "i", "500")])
    g = generalize_tape(tape, crashes_if_big)
    assert g.reproduced
    assert len(g.boundaries) == 1
    b = g.boundaries[0]
    assert b.op == ">=" and b.threshold == 95 and b.passing_example == 94
    assert g.as_property() == "assert n < 95"


def test_boundary_is_exact_le():
    tape = flight.Tape([(0, "random.randint", "i", "0")])
    g = generalize_tape(tape, crashes_if_small)
    assert g.reproduced
    b = g.boundaries[0]
    assert b.op == "<=" and b.threshold == 10 and b.passing_example == 11
    assert g.as_property() == "assert n > 10"


def test_passing_example_does_not_reproduce():
    tape = flight.Tape([(0, "random.randint", "i", "500")])
    g = generalize_tape(tape, crashes_if_big)
    passing = g.boundaries[0].passing_example
    edited = flight.Tape([(0, "random.randint", "i", str(passing))])
    result = flight.replay_tape(edited, crashes_if_big)
    assert result == passing


def test_not_reproduced_when_value_harmless():
    tape = flight.Tape([(0, "random.randint", "i", "10")])
    g = generalize_tape(tape, crashes_if_big)
    assert not g.reproduced
    assert "did not reproduce" in g.render()


def test_float_boundary():
    def crashes_if_hot():
        import time

        t = time.time()
        if t >= 3.5:
            raise ValueError("hot")
        return t

    tape = flight.Tape([(0, "time.time", "f", "10.0")])
    g = generalize_tape(tape, crashes_if_hot)
    assert g.reproduced
    b = g.boundaries[0]
    assert b.op == ">="
    assert abs(b.threshold - 3.5) < 1e-6


def test_hypothesis_scaffold_shape():
    tape = flight.Tape([(0, "random.randint", "i", "500")])
    g = generalize_tape(tape, crashes_if_big)
    scaffold = g.as_hypothesis("target")
    assert "from hypothesis import" in scaffold
    assert "assume(n < 95)" in scaffold
    assert "@given(n=st.integers())" in scaffold


def module_level_flaky():
    n = random.randint(0, 100)
    if n >= 50:
        raise ValueError(f"boom {n}")
    return n


def _record_flaky_crash(path):
    for _ in range(200):
        try:
            with flight.deterministic(str(path)):
                module_level_flaky()
        except ValueError:
            return str(path)
    raise AssertionError("never crashed — vanishingly unlikely at 50%")


def test_generalize_resolves_fn_from_recording(tmp_path):
    p = _record_flaky_crash(tmp_path / "c.flight")
    g = generalize(p)
    assert isinstance(g, Generalization)
    assert g.reproduced
    assert g.boundaries
    b = g.boundaries[0]
    assert b.op == ">=" and b.threshold == 50


def test_generalize_no_crash_recording(tmp_path):
    with flight.deterministic(str(tmp_path / "clean.flight")):
        random.randint(0, 10)
    g = generalize(str(tmp_path / "clean.flight"))
    assert not g.reproduced


def test_cli_generalize(tmp_path):
    p = _record_flaky_crash(tmp_path / "c.flight")
    proc = subprocess.run(
        [sys.executable, "-m", "flight", "generalize", p, "--property"],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert "fails when ≥ 50" in proc.stdout
    assert "candidate property" in proc.stdout


def test_cli_generalize_hypothesis(tmp_path):
    p = _record_flaky_crash(tmp_path / "c.flight")
    out = tmp_path / "test_prop.py"
    proc = subprocess.run(
        [sys.executable, "-m", "flight", "generalize", p, "--hypothesis", "-o", str(out)],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert out.exists()
    assert "assume(n < 50)" in out.read_text()
