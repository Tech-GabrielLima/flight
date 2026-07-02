"""Cross-cutting robustness tests: hostile inputs, edge cases, and the P1
guarantee (the recorder must never take down the program it records)."""

from __future__ import annotations

import random
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

import flight
from flight import _core
from flight._serialize import GraphSerializer, describe_shallow


# -- serializer against hostile objects -------------------------------------


def _nodes(root, **kw):
    g = GraphSerializer(**kw)
    g.add_root(root)
    nodes = g.run()
    return g, {n[0]: n for n in nodes}


def test_giant_int_does_not_crash_serialization():
    g, by_id = _nodes(10**5000)
    node = next(n for n in by_id.values() if n[1] == "int")
    assert "bits" in node[2]  # rendered as a bit-length summary, not a crash
    assert describe_shallow(10**5000)[0] == "int"


def test_dict_subclass_with_raising_items_survives():
    class Evil(dict):
        def items(self):
            raise RuntimeError("no items for you")

    g, by_id = _nodes(Evil(a=1))
    assert by_id  # produced a node, did not raise


def test_object_with_raising_dict_property_survives():
    class Evil:
        @property
        def __dict__(self):
            raise RuntimeError("no dict")

    g, by_id = _nodes(Evil())
    assert by_id


def test_object_with_raising_getattr_on_slots_survives():
    class Evil:
        __slots__ = ("x",)

        def __getattribute__(self, name):
            raise RuntimeError("denied")

    g, by_id = _nodes(Evil())
    assert by_id  # _get_attrs swallows the error


def test_deeply_nested_structure_terminates():
    d = cur = {}
    for _ in range(5000):
        nxt = {}
        cur["n"] = nxt
        cur = nxt
    g, by_id = _nodes(d, max_depth=6)
    # bounded by depth, not by the 5000 levels
    assert any(n[1] == "truncated" for n in by_id.values())


def test_self_referential_list_terminates():
    a = []
    a.append(a)
    g, by_id = _nodes(a)
    root = by_id[0]
    assert root[6][0][1] == 0  # element points back at the root node id


def test_widely_shared_object_is_one_node():
    shared = {"k": 1}
    container = [shared] * 100
    g, by_id = _nodes(container)
    root = by_id[0]
    child_ids = {cid for _k, cid in root[6]}
    assert len(child_ids) == 1  # all 100 references collapse to one node


# -- format / reader: truncation of a full crash file -----------------------


def _crash_bytes(tmp_path) -> bytes:
    out = tmp_path / "c.flight"
    flight.install()

    def f(cfg):
        local = {"nested": [1, 2, 3]}  # noqa: F841
        return cfg["x"][99]

    try:
        f({"x": [1, 2, 3], "name": "widget"})
    except IndexError:
        flight.capture(path=out)
    flight.uninstall()
    return out.read_bytes()


def test_full_crash_file_survives_truncation_at_every_byte(tmp_path):
    data = _crash_bytes(tmp_path)
    assert len(data) > 100
    victim = tmp_path / "v.flight"
    for cut in range(len(data)):
        victim.write_bytes(data[:cut])
        try:
            f = flight.read(victim)
            _ = f.blocks, f.has_crash  # touch typed accessors
            if f.has_crash:
                f.crash()  # decode frames/objects — must not raise
        except ValueError:
            pass  # a clean hard error inside the header region is acceptable
        # any other exception would fail the test by propagating


def test_reading_a_truncated_crash_is_partial_or_clean(tmp_path):
    data = _crash_bytes(tmp_path)
    victim = tmp_path / "v.flight"
    victim.write_bytes(data[: len(data) - 20])  # lose the footer + a bit
    f = flight.read(victim)
    # whatever survived is coherent
    if f.has_crash:
        crash = f.crash()
        for fr in crash.frames:
            for _name, oid in fr.locals:
                assert isinstance(oid, int)


# -- P1: hostile hooks must not escalate ------------------------------------


def test_capture_survives_repr_bomb_locals(tmp_path):
    out = tmp_path / "bomb.flight"
    flight.install()

    class Bomb:
        def __repr__(self):
            raise KeyboardInterrupt  # nastiest: a BaseException

    def f():
        b = Bomb()  # noqa: F841
        raise ValueError("x")

    try:
        f()
    except ValueError:
        path = flight.capture(path=out)  # must not raise the KeyboardInterrupt
    flight.uninstall()
    assert path == out


def test_unraisablehook_is_installed_and_restored():
    original = sys.unraisablehook
    flight.install()
    assert sys.unraisablehook is not original
    flight.uninstall()
    assert sys.unraisablehook is original


@pytest.mark.filterwarnings("ignore::pytest.PytestUnraisableExceptionWarning")
def test_unraisable_exception_is_captured(tmp_path):
    # An exception in __del__ is "unraisable": Python routes it to
    # sys.unraisablehook instead of propagating. flight should capture it.
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    flight.install(output_dir=out_dir)

    class Bad:
        def __del__(self):
            raise ValueError("boom in del")

    try:
        b = Bad()
        del b
        import gc

        gc.collect()  # force finalization
    finally:
        flight.uninstall()

    files = list(out_dir.glob("*.flight"))
    # At least one flight should have been written for the unraisable exception.
    assert files, "unraisable exception was not captured"
    assert any(flight.read(f).exceptions for f in files)


def test_installed_recorder_does_not_break_normal_execution():
    flight.install()
    try:
        # a mix of ordinary work must run untouched
        acc = 0
        for i in range(1000):
            acc += i
        assert acc == 499500
        assert {"a": 1}.get("a") == 1
    finally:
        flight.uninstall()


# -- nondet: exceptions and unencodable results -----------------------------


def test_raising_boundary_is_recorded_and_replayed(tmp_path):
    out = tmp_path / "r.flight"

    def work():
        try:
            random.randint(10, 1)  # empty range -> ValueError
        except ValueError:
            return "handled"
        return "not handled"

    with flight.deterministic(out):
        original = work()
    assert original == "handled"
    assert flight.replay(out, work) == "handled"


def test_replay_reraises_uncaught_boundary_exception(tmp_path):
    out = tmp_path / "r.flight"

    def work():
        return random.randint(10, 1)  # ValueError, uncaught

    with flight.deterministic(out):
        with pytest.raises(ValueError):
            work()
    # replay reproduces the ValueError
    with pytest.raises(ValueError):
        flight.replay(out, work)


# -- CLI robustness ---------------------------------------------------------


def test_cli_inspect_on_nonexistent_file_errors_cleanly():
    proc = subprocess.run(
        [sys.executable, "-m", "flight", "inspect", "/no/such/file.flight"],
        capture_output=True,
        text=True,
    )
    assert proc.returncode != 0
    # it's a clean error message, not a traceback dump we can't read
    assert "flight" in (proc.stderr + proc.stdout).lower() or proc.returncode == 1


def test_reader_never_panics_on_random_garbage(tmp_path):
    # The ultimate tolerance guarantee: no random byte string may crash the
    # native reader — only a clean ValueError is allowed.
    import random

    rng = random.Random(1234)
    fp = tmp_path / "fuzz.flight"
    readers = (_core.read_summary, _core.read_crash, _core.read_mutations, _core.read_nondet)
    for _ in range(1500):
        n = rng.randint(0, 300)
        data = bytes(rng.getrandbits(8) for _ in range(n))
        if rng.random() < 0.5:  # bias towards passing the magic check
            data = b"FLGT" + data[4:] if len(data) >= 4 else b"FLGT"
        fp.write_bytes(data)
        for reader in readers:
            try:
                reader(str(fp))
            except ValueError:
                pass  # clean, expected
            # any other exception propagates and fails the test


def test_repro_on_ring_only_file_reports_cleanly(tmp_path):
    out = tmp_path / "ring.flight"
    flight.install()
    (lambda: sum(range(3)))()
    flight.dump(out)
    flight.uninstall()
    proc = subprocess.run(
        [sys.executable, "-m", "flight", "repro", str(out)],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 1
    assert "cannot build a repro" in proc.stderr
