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
