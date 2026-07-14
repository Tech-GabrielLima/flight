from __future__ import annotations

import subprocess
import sys

import flight
from flight._slice import Slice, backward_slice


def compute_average(numbers):
    total = 0
    for n in numbers:
        total += n
    return total / len(numbers)


def summarize(datasets):
    results = []
    for name, data in datasets.items():
        avg = compute_average(data)
        results.append((name, avg))
    return results


def scenario():
    datasets = {"morning": [10, 20, 30], "evening": []}
    return summarize(datasets)


def _crash(path):
    flight.install()
    try:
        scenario()
    except ZeroDivisionError:
        flight.capture(path=str(path))
    finally:
        flight.uninstall()
    return str(path)


def test_slice_traces_empty_list_to_its_origin(tmp_path):
    p = _crash(tmp_path / "c.flight")
    sl = flight.why(p, frame=0, var="numbers")
    assert isinstance(sl, Slice)
    assert sl.hops
    text = sl.render()
    assert any(h.reason == "alias" and h.var == "data" for h in sl.hops)
    assert any(h.reason == "contained-in" and "evening" in h.detail for h in sl.hops)
    assert "root" in text
    assert "scenario" in sl.root


def test_slice_marks_parameter(tmp_path):
    p = _crash(tmp_path / "c.flight")
    sl = backward_slice(flight.read(p), frame=0, var="numbers")
    assert sl.hops[0].reason == "param"


def test_slice_computed_value_names_its_reads(tmp_path):
    p = _crash(tmp_path / "c.flight")
    sl = flight.why(p, frame=0, var="total")
    assert sl.hops
    assert sl.hops[0].reason in ("seed", "write")
    assert sl.hops[0].source_line.strip().startswith("total")


def test_slice_no_such_local(tmp_path):
    p = _crash(tmp_path / "c.flight")
    sl = flight.why(p, frame=0, var="does_not_exist")
    assert not sl.hops
    assert "no such local" in sl.value


def test_slice_no_crash_frames(tmp_path):
    flight.install()
    try:
        p = str(tmp_path / "snap.flight")
        flight.capture(path=p)
    finally:
        flight.uninstall()
    sl = flight.why(p, frame=0, var="x")
    assert not sl.hops


def test_slice_max_hops_truncates(tmp_path):
    p = _crash(tmp_path / "c.flight")
    sl = flight.why(p, frame=0, var="numbers", max_hops=1)
    assert sl.truncated


def test_slice_render_is_stable(tmp_path):
    p = _crash(tmp_path / "c.flight")
    r1 = flight.why(p, frame=0, var="numbers").render()
    r2 = flight.why(p, frame=0, var="numbers").render()
    assert r1 == r2
    assert "how this value came to be" in r1


def test_slice_uses_mutation_writes(tmp_path):
    def run():
        acc = 0
        for i in range(3):
            acc += i
        return acc / 0

    p = str(tmp_path / "scope.flight")
    flight.install()
    try:
        with flight.record(path=p):
            run()
    except ZeroDivisionError:
        pass
    finally:
        flight.uninstall()
    fl = flight.read(p)
    sl = fl.why(frame=0, var="acc")
    assert isinstance(sl, Slice)


def test_cli_why(tmp_path):
    p = _crash(tmp_path / "c.flight")
    proc = subprocess.run(
        [sys.executable, "-m", "flight", "why", p, "--var", "numbers"],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0
    assert "how this value came to be" in proc.stdout
    assert "SAME object" in proc.stdout


def test_cli_why_no_crash(tmp_path):
    flight.install()
    try:
        p = str(tmp_path / "snap.flight")
        flight.capture(path=p)
    finally:
        flight.uninstall()
    proc = subprocess.run(
        [sys.executable, "-m", "flight", "why", p, "--var", "x"],
        capture_output=True, text=True,
    )
    assert proc.returncode == 1
    assert "no crash" in proc.stderr
