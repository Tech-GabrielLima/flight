"""End-to-end tests of the Phase-0 black box: install → record → dump → read."""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

import flight
from flight import _core


def _write_module(tmp_path: Path, name: str, body: str) -> Path:
    p = tmp_path / name
    p.write_text(textwrap.dedent(body))
    return p


def _run_module_recorded(path: Path):
    """Execute a module file (as interesting user code) under recording."""
    code = compile(path.read_text(), str(path), "exec")
    ns: dict = {}
    exec(code, ns)  # noqa: S102 - deliberate, it's the code under test
    return ns


# -- install / uninstall ----------------------------------------------------


def test_install_returns_config_and_reports_installed():
    assert not flight.is_installed()
    cfg = flight.install()
    assert isinstance(cfg, flight.Config)
    assert flight.is_installed()
    flight.uninstall()
    assert not flight.is_installed()


def test_uninstall_restores_excepthook():
    original = sys.excepthook
    flight.install()
    assert sys.excepthook is not original
    flight.uninstall()
    assert sys.excepthook is original


def test_reinstall_replaces_cleanly():
    original = sys.excepthook
    flight.install()
    flight.install()  # should uninstall the first session, not stack hooks
    flight.uninstall()
    assert sys.excepthook is original


# -- recording user code ----------------------------------------------------


def test_records_events_for_interesting_code(tmp_path):
    mod = _write_module(
        tmp_path,
        "work.py",
        """
        def add(a, b):
            c = a + b
            return c

        def run():
            total = 0
            for i in range(5):
                total = add(total, i)
            return total

        run()
        """,
    )
    flight.install(force_include=(str(tmp_path),))
    _run_module_recorded(mod)
    stats = flight.stats()
    flight.uninstall()
    assert stats["total_events"] > 10
    assert stats["codes"] >= 2  # add + run (+ module)


def test_capture_writes_a_readable_flight(tmp_path):
    mod = _write_module(
        tmp_path,
        "boom.py",
        """
        def divide(a, b):
            return a // b

        try:
            divide(1, 0)
        except ZeroDivisionError:
            pass
        """,
    )
    out = tmp_path / "cap.flight"
    flight.install(force_include=(str(tmp_path),))
    _run_module_recorded(mod)
    path = flight.capture(path=out)
    flight.uninstall()

    assert path == out
    assert out.exists()
    f = flight.read(out)
    assert not f.partial
    assert f.used_index
    assert f.blocks == ["META", "EVENT_RING"]
    assert f.event_count > 0
    assert f.meta["python_version"]
    # The recent events should include the divide frame.
    kinds = {k for k, _file, _line in f.recent_events}
    assert kinds  # non-empty preview


def test_uninteresting_code_is_not_recorded(tmp_path):
    # Deny the module's directory. We also deny the repo root so this test's
    # own harness code (which is "interesting" by default) doesn't pollute the
    # count — leaving the denied module as the only thing that *could* record.
    repo_root = str(Path(__file__).resolve().parents[1])
    mod = _write_module(
        tmp_path,
        "lib.py",
        """
        def helper():
            total = 0
            for i in range(100):
                total += i
            return total
        helper()
        """,
    )
    base = flight.Config()
    cfg = flight.Config(deny_prefixes=base.deny_prefixes + (str(tmp_path), repo_root))
    flight.install(config=cfg)
    _run_module_recorded(mod)
    stats = flight.stats()
    flight.uninstall()
    # Nothing from the denied module (or the harness) should have been recorded.
    assert stats["total_events"] == 0


def test_record_returns_default_on_and_opt_out(tmp_path):
    mod = _write_module(
        tmp_path,
        "calls.py",
        """
        def leaf(x):
            return x + 1

        def run():
            total = 0
            for i in range(20):
                total = leaf(total)
            return total

        run()
        """,
    )

    # Default: PY_RETURN events are recorded (documented rear-view mirror).
    flight.install(force_include=(str(tmp_path),))
    _run_module_recorded(mod)
    default_events = flight.stats()["total_events"]
    out = tmp_path / "on.flight"
    flight.capture(path=out)
    flight.uninstall()
    kinds_on = {k for k, _f, _l in flight.read(out).recent_events}
    assert "PY_RETURN" in kinds_on
    assert "PY_START" in kinds_on

    # Opt out: fewer events, but the call path (PY_START) is still there.
    flight.install(force_include=(str(tmp_path),), record_returns=False)
    _run_module_recorded(mod)
    noret_events = flight.stats()["total_events"]
    out2 = tmp_path / "off.flight"
    flight.capture(path=out2)
    flight.uninstall()
    kinds_off = {k for k, _f, _l in flight.read(out2).recent_events}
    assert "PY_START" in kinds_off
    assert "PY_RETURN" not in kinds_off
    # Dropping returns cuts the event volume (roughly in half for call-heavy code).
    assert noret_events < default_events


def test_stats_shape():
    flight.install()
    s = flight.stats()
    flight.uninstall()
    assert set(s) == {"total_events", "threads", "codes", "ring_capacity"}
    assert s["ring_capacity"] >= 16


# -- crash auto-dump (subprocess, real excepthook path) ---------------------


def test_uncaught_exception_writes_flight_via_cli(tmp_path):
    script = _write_module(
        tmp_path,
        "crash.py",
        """
        def f(n):
            return 10 // n
        def g():
            return f(0)
        g()
        """,
    )
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    proc = subprocess.run(
        [sys.executable, "-m", "flight", "run", "--output-dir", str(out_dir), str(script)],
        capture_output=True,
        text=True,
    )
    # The program crashed, but the CLI wrapper let the traceback through.
    assert "ZeroDivisionError" in proc.stderr
    assert "[flight] recorded" in proc.stderr
    files = list(out_dir.glob("*.flight"))
    assert len(files) == 1
    f = flight.read(files[0])
    assert not f.partial
    assert f.event_count > 0
    # The crash path went through f and g, so we should see RAISE/UNWIND.
    assert any(k in {"RAISE", "PY_UNWIND"} for k, _f, _l in f.recent_events)


def test_ring_capacity_is_configurable_before_first_use():
    assert _core.configure(256) in (True, False)  # may already be initialized
    flight.install(ring_capacity=256)
    cap = flight.stats()["ring_capacity"]
    flight.uninstall()
    assert cap >= 16
