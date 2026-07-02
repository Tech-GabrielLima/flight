"""Phase-2 scope recording: `with flight.record()`, mutation log, timeline."""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

import flight


def test_records_local_variable_history(tmp_path):
    out = tmp_path / "s.flight"
    with flight.record(path=out):
        total = 0
        for i in range(4):
            total = total + i * 10  # 0, 10, 30, 60

    f = flight.read(out)
    assert not f.partial
    assert "MUTATION" in f.blocks
    assert f.has_mutations

    rec = f.recording()
    history = [m.value_repr for m in rec.history("total")]
    # initial 0, then 0, 10, 30, 60 (the first assignment then each update)
    assert history[0] == "0"
    assert history[-1] == "60"
    assert "10" in history and "30" in history


def test_state_at_reconstructs_locals(tmp_path):
    out = tmp_path / "s.flight"
    with flight.record(path=out):
        a = 1
        b = 2
        a = a + b  # a -> 3

    rec = flight.read(out).recording()
    # at the very end, a == 3 and b == 2
    final = rec.state_at(rec.mutations[-1].seq)
    assert final["a"] == "3"
    assert final["b"] == "2"
    # at the first write of a (seq of first 'a' local), a == 1 and b not yet set
    first_a = rec.history("a")[0]
    early = rec.state_at(first_a.seq)
    assert early["a"] == "1"
    assert "b" not in early


def test_watch_dict_records_item_writes(tmp_path):
    out = tmp_path / "w.flight"
    with flight.record(path=out) as rec:
        cache: dict = {}
        rec.watch(cache, name="cache")
        for i in range(3):
            cache[i] = i * i  # cache[0]=0, cache[1]=1, cache[2]=4

    r = flight.read(out).recording()
    writes = r.who_mutated("cache")
    keys = {m.key for m in writes}
    assert {"0", "1", "2"} <= keys
    # the value written for key 2 was 4
    v2 = [m for m in writes if m.key == "2"][-1]
    assert v2.value_repr == "4"


def test_module_level_watch_helper_is_noop_outside_scope():
    d = {}
    # Outside any scope, watch() just returns the object.
    assert flight.watch(d) is d


def test_watch_via_module_helper_inside_scope(tmp_path):
    out = tmp_path / "w2.flight"
    with flight.record(path=out):
        data = {}
        flight.watch(data, name="data")
        data["x"] = 99

    r = flight.read(out).recording()
    writes = r.who_mutated("data")
    assert any(m.key == "x" and m.value_repr == "99" for m in writes)


def test_scrubbing_in_scope_locals(tmp_path):
    out = tmp_path / "sc.flight"
    with flight.record(path=out):
        username = "alice"
        password = "hunter2"  # noqa: F841

    rec = flight.read(out).recording()
    assert rec.history("username")[-1].value_repr == "alice"
    assert rec.history("password")[-1].value_repr == "<redacted>"


def test_exception_inside_scope_still_writes_recording(tmp_path):
    out = tmp_path / "boom.flight"
    with pytest.raises(ZeroDivisionError):
        with flight.record(path=out):
            step = 0
            step = 1  # noqa: F841
            1 / 0

    # The recording up to the crash is on disk.
    rec = flight.read(out).recording()
    assert [m.value_repr for m in rec.history("step")][:2] == ["0", "1"]


def test_record_auto_installs_when_not_installed(tmp_path):
    out = tmp_path / "auto.flight"
    assert not flight.is_installed()
    with flight.record(path=out):
        x = 5  # noqa: F841
    # auto-installed session is torn down again
    assert not flight.is_installed()
    assert flight.read(out).has_mutations


def test_record_within_existing_session_preserves_it(tmp_path):
    out = tmp_path / "nested.flight"
    flight.install()
    try:
        with flight.record(path=out):
            y = 7  # noqa: F841
        assert flight.is_installed()  # our session survived the scope
    finally:
        flight.uninstall()
    assert flight.read(out).has_mutations


def test_last_write_in_a_frame_is_captured(tmp_path):
    # Regression: a write on a function's LAST line has no trailing LINE event,
    # so it must be recovered at PY_RETURN — otherwise it would be lost.
    out = tmp_path / "last.flight"

    def compute():
        a = 10
        b = 20
        result = a + b  # last line of the frame -> result must be captured
        return result

    with flight.record(path=out):
        compute()

    rec = flight.read(out).recording()
    assert [m.value_repr for m in rec.history("result")] == ["30"]


def test_nested_frame_only_line_is_captured(tmp_path):
    out = tmp_path / "nested2.flight"

    def leaf():
        only = "value"  # single line: captured at PY_RETURN

    def caller():
        leaf()

    with flight.record(path=out):
        caller()

    rec = flight.read(out).recording()
    assert [m.value_repr for m in rec.history("only")] == ["value"]


def test_line_attribution_is_exact(tmp_path):
    out = tmp_path / "lines.flight"

    def f():
        first = 1  # noqa: F841
        second = 2  # noqa: F841

    first_line = f.__code__.co_firstlineno
    with flight.record(path=out):
        f()

    rec = flight.read(out).recording()
    by_name = {m.name: m for m in rec.history("first") + rec.history("second")}
    # `first = 1` is the 2nd line of f, `second = 2` the 3rd — exact attribution.
    assert by_name["first"].line == first_line + 1
    assert by_name["second"].line == first_line + 2


def test_reassignment_history_is_ordered_and_complete(tmp_path):
    out = tmp_path / "reassign.flight"

    def run():
        n = 0
        n = 1
        n = 2
        n = 3
        return n

    with flight.record(path=out):
        run()

    rec = flight.read(out).recording()
    assert [m.value_repr for m in rec.history("n")] == ["0", "1", "2", "3"]


def test_nested_scopes_each_get_their_own_recording(tmp_path):
    outer = tmp_path / "outer.flight"
    inner = tmp_path / "inner.flight"
    with flight.record(path=outer):
        a = 1  # noqa: F841
        with flight.record(path=inner):
            b = 2  # noqa: F841
        c = 3  # noqa: F841

    inner_names = flight.read(inner).recording().names()
    outer_names = flight.read(outer).recording().names()
    assert "b" in inner_names
    assert "a" in outer_names  # both scopes recorded independently


def test_watch_tracks_object_attribute_writes(tmp_path):
    out = tmp_path / "attr.flight"

    class Box:
        pass

    with flight.record(path=out) as rec:
        box = Box()
        rec.watch(box, name="box")
        box.value = 10
        box.value = 20

    writes = flight.read(out).recording().who_mutated("box")
    reprs = [m.value_repr for m in writes if m.key == "value"]
    assert reprs == ["10", "20"]


def test_recursion_frames_are_distinguished(tmp_path):
    out = tmp_path / "rec.flight"

    def countdown(n):
        marker = n * 10  # noqa: F841
        if n > 0:
            countdown(n - 1)

    with flight.record(path=out):
        countdown(3)

    rec = flight.read(out).recording()
    marker_writes = rec.history("marker")
    # one 'marker' write per recursive frame (distinct frame ids)
    frames = {m.frame for m in marker_writes}
    assert len(frames) >= 3


def test_cli_timeline(tmp_path):
    script = tmp_path / "prog.py"
    script.write_text(
        textwrap.dedent(
            """
            import flight
            with flight.record(path="OUT"):
                acc = 0
                for i in range(3):
                    acc += i
            """
        ).replace("OUT", str(tmp_path / "t.flight"))
    )
    subprocess.run([sys.executable, str(script)], capture_output=True, text=True, check=True)
    out = tmp_path / "t.flight"
    proc = subprocess.run(
        [sys.executable, "-m", "flight", "timeline", "--var", "acc", str(out)],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0
    assert "history of local 'acc'" in proc.stdout
    assert "acc =" in proc.stdout
