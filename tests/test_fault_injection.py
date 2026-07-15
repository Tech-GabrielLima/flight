"""Reliability under violent death, enforced in CI.

A small, fast version of benchmarks/fault_injection.py: kill a writer with
SIGKILL while it is writing, and truncate a real recording at every byte
offset — asserting the reader never crashes, only ever parsing, going partial,
or erroring gracefully. The full run (thousands of kills) lives in benchmarks/.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time

import pytest

import flight


def _record_crash(path, obj_size=200):
    flight.install(record_lines=True)
    try:
        data = {i: {"v": list(range(8)), "n": f"x{i}"} for i in range(obj_size)}  # noqa: F841
        _ = 1 / len([])
    except ZeroDivisionError:
        flight.capture(path=str(path))
    finally:
        flight.uninstall()
    return str(path)


def test_reader_tolerates_every_truncation(tmp_path):
    """Every byte prefix of a real .flight is complete, partial, or a graceful
    error — never a crash. This is the exhaustive superset of what any kill can
    leave on disk (writes are append-only)."""
    full = _record_crash(tmp_path / "c.flight")
    data = open(full, "rb").read()
    assert len(data) > 200
    prefix = tmp_path / "p.flight"
    seen = set()
    for cut in range(0, len(data) + 1):
        prefix.write_bytes(data[:cut])
        try:
            fl = flight.read(str(prefix))
            # force the lazy parse to actually touch the (possibly cut) blocks
            if fl.has_crash:
                c = fl.crash()
                _ = [f.locals for f in c.frames]
            seen.add("partial" if fl.partial else "complete")
        except Exception:  # a truncated header/magic is expected and fine
            seen.add("graceful")
    # completing the loop at all means no truncation crashed the reader
    assert "partial" in seen  # mid-file cuts really did degrade, not error out


@pytest.mark.skipif(sys.platform == "win32", reason="SIGKILL semantics differ on Windows")
def test_sigkill_during_write_leaves_readable_file(tmp_path):
    """kill -9 a process that is writing a .flight in a loop; whatever is on
    disk must still be readable (never crash the reader)."""
    writer = tmp_path / "writer.py"
    out = tmp_path / "k.flight"
    writer.write_text(
        "import flight, sys\n"
        "flight.install(record_lines=True)\n"
        "try:\n"
        "    small = {i: list(range(6)) for i in range(250)}\n"  # fast even in a debug build
        "    _ = 1 / len([])\n"
        "except ZeroDivisionError:\n"
        "    sys.stdout.write('GO\\n'); sys.stdout.flush()\n"
        "    for _ in range(100000):\n"
        f"        flight.capture(path=r'{out}')\n"
    )
    crashes = 0
    files_seen = 0
    for i in range(25):
        if out.exists():
            out.unlink()
        p = subprocess.Popen([sys.executable, str(writer)], stdout=subprocess.PIPE, text=True)
        p.stdout.readline()  # wait for GO (writing loop started)
        # spread kills from prompt to ~1.3s so at least some land after a capture
        # completes even on a slow (debug) build
        time.sleep(0.05 + (i % 13) * 0.1)
        p.send_signal(signal.SIGKILL)
        p.wait()
        if out.exists() and out.stat().st_size > 0:
            files_seen += 1
            # read in a subprocess so a hypothetical abort is a non-zero exit
            r = subprocess.run(
                [sys.executable, "-c",
                 "import flight,sys;fl=flight.read(sys.argv[1]);"
                 "print('P' if fl.partial else 'C');"
                 "(fl.crash().frames if fl.has_crash else None)", str(out)],
                capture_output=True, text=True, timeout=30,
            )
            if r.returncode != 0:
                crashes += 1
    assert crashes == 0, f"{crashes} reader crash(es) on SIGKILL-produced files"
    assert files_seen > 0, "no on-disk files were produced to validate"
