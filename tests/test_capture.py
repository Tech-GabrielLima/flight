"""End-to-end Phase-1 crash capture: frames, locals, object graph, exception
chain, source, aliasing, scrubbing — via the real capture path."""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

import flight


def _find_frame(crash, qualname):
    for fr in crash.frames:
        if fr.qualname == qualname or fr.qualname.endswith("." + qualname):
            return fr
    raise AssertionError(f"frame {qualname!r} not found in {[f.qualname for f in crash.frames]}")


def _local(crash, frame, name):
    oid = dict(frame.locals)[name]
    return crash.objects[oid]


def test_capture_handled_exception_has_frames_and_locals(tmp_path):
    out = tmp_path / "c.flight"
    flight.install()

    def divide(a, b):
        scale = 10
        return (a * scale) // b

    def run():
        numerator = 7
        divide(numerator, 0)

    try:
        run()
    except ZeroDivisionError:
        flight.capture(path=out)
    flight.uninstall()

    f = flight.read(out)
    assert not f.partial
    assert set(["META", "EXCEPTION", "FRAME", "OBJECT", "EVENT_RING"]).issubset(set(f.blocks))
    assert f.exceptions[0][0] == "ZeroDivisionError"
    assert f.has_crash

    crash = f.crash()
    # crash-first ordering: divide is the innermost frame.
    assert crash.frames[0].qualname.endswith("divide")
    divide_frame = _find_frame(crash, "divide")
    assert _local(crash, divide_frame, "scale")["repr"] == "10"
    assert _local(crash, divide_frame, "b")["repr"] == "0"


def test_capture_records_object_graph_and_aliasing(tmp_path):
    out = tmp_path / "alias.flight"
    flight.install()

    def inner(cfg):
        raise ValueError("stop")

    def outer():
        config = {"mode": "prod"}
        inner(config)

    try:
        outer()
    except ValueError:
        flight.capture(path=out)
    flight.uninstall()

    crash = flight.read(out).crash()
    inner_f = _find_frame(crash, "inner")
    outer_f = _find_frame(crash, "outer")
    cfg_id = dict(inner_f.locals)["cfg"]
    config_id = dict(outer_f.locals)["config"]
    assert cfg_id == config_id  # same dict, aliased across frames
    # aliases() reports both appearances
    appearances = {name for _i, name in crash.aliases(cfg_id)}
    assert {"cfg", "config"} <= appearances


def test_capture_scrubs_sensitive_locals(tmp_path):
    out = tmp_path / "scrub.flight"
    flight.install()

    def login():
        username = "alice"
        password = "s3cr3t"
        raise RuntimeError("auth failed")

    try:
        login()
    except RuntimeError:
        flight.capture(path=out)
    flight.uninstall()

    crash = flight.read(out).crash()
    login_f = _find_frame(crash, "login")
    assert _local(crash, login_f, "username")["repr"] == "alice"
    assert _local(crash, login_f, "password")["repr"] == "<redacted>"


def test_capture_exception_chain(tmp_path):
    out = tmp_path / "chain.flight"
    flight.install()

    def boom():
        try:
            raise KeyError("missing")
        except KeyError as e:
            raise ValueError("wrapped") from e

    try:
        boom()
    except ValueError:
        flight.capture(path=out)
    flight.uninstall()

    excs = flight.read(out).exceptions
    assert excs[0][0] == "ValueError"
    assert excs[1][0] == "KeyError"
    assert excs[1][2] == "cause"  # raised "from e"


def test_capture_includes_source(tmp_path):
    # A real script file so linecache can read its source.
    script = tmp_path / "prog.py"
    script.write_text(
        textwrap.dedent(
            """
            def f():
                x = 1
                return x / 0
            f()
            """
        )
    )
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    proc = subprocess.run(
        [sys.executable, "-m", "flight", "run", "--output-dir", str(out_dir), str(script)],
        capture_output=True,
        text=True,
    )
    assert "ZeroDivisionError" in proc.stderr
    files = list(out_dir.glob("*.flight"))
    assert len(files) == 1
    crash = flight.read(files[0]).crash()
    # the source of prog.py was captured
    assert any(str(script) in name for name in crash.sources)
    assert "return x / 0" in "\n".join(crash.sources.values())


def test_capture_without_active_exception_falls_back_to_ring(tmp_path):
    out = tmp_path / "ring.flight"
    flight.install()

    def loop():
        return sum(range(10))

    loop()
    path = flight.capture(path=out)  # no active exception
    flight.uninstall()

    f = flight.read(path)
    assert "EVENT_RING" in f.blocks
    assert not f.has_crash  # ring-only, no frames/exception


def test_capture_never_raises_on_evil_locals(tmp_path):
    out = tmp_path / "evil.flight"
    flight.install()

    class Evil:
        def __repr__(self):
            raise RuntimeError("no repr for you")

    def f():
        bad = Evil()  # noqa: F841
        raise ValueError("x")

    try:
        f()
    except ValueError:
        path = flight.capture(path=out)  # must not raise
    flight.uninstall()
    assert path == out
    assert flight.read(out).has_crash
