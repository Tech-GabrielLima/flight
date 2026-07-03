"""Phase 8 — the production black box: correlation, overhead governor, and a
crash-surviving supervisor daemon.

The decision logic is pure and unit-tested with injected clocks/stats; the
daemon is exercised end-to-end by SIGKILL-ing a real child process and asserting
a `.flight` still appears (a black box that survives the death of the plane).
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

import flight
from flight._correlation import (
    Link,
    TraceContext,
    resolve,
    trace_graph,
)
from flight._governor import (
    LEVEL_CALLS,
    LEVEL_LINES,
    LEVEL_RETURNS,
    Governor,
    OverheadLadder,
    estimate_overhead,
)

VALID_TP = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"


# =========================================================================
# Distributed correlation
# =========================================================================


def test_traceparent_parses_and_round_trips():
    ctx = TraceContext.parse(VALID_TP)
    assert ctx.trace_id == "4bf92f3577b34da6a3ce929d0e0e4736"
    assert ctx.span_id == "00f067aa0ba902b7"
    assert ctx.flags == 1 and ctx.sampled
    assert ctx.traceparent() == VALID_TP


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "garbage",
        "00-tooShort-00f067aa0ba902b7-01",
        f"00-{'0' * 32}-00f067aa0ba902b7-01",  # all-zero trace id
        f"00-4bf92f3577b34da6a3ce929d0e0e4736-{'0' * 16}-01",  # all-zero span
        "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7",  # missing field
        "zz-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",  # bad version
    ],
)
def test_traceparent_rejects_malformed(bad):
    assert TraceContext.parse(bad) is None


def test_context_survives_the_nondet_tape():
    ctx = (
        TraceContext.parse(VALID_TP)
        .with_service("checkout")
        .with_link(Link("4bf92f3577b34da6a3ce929d0e0e4736", "svcA.flight", "gateway"))
    )
    rows = ctx.to_nondet()
    back = TraceContext.from_nondet(rows)
    assert back.trace_id == ctx.trace_id
    assert back.span_id == ctx.span_id
    assert back.service == "checkout"
    assert [(l.ref, l.service) for l in back.links] == [("svcA.flight", "gateway")]


def test_from_nondet_returns_none_without_a_context():
    assert TraceContext.from_nondet([(0, "time.time", "f", "1.0")]) is None


def test_resolve_precedence_env_and_explicit(monkeypatch):
    monkeypatch.setenv("TRACEPARENT", VALID_TP)
    monkeypatch.setenv("OTEL_SERVICE_NAME", "from-env")
    # env is used when no explicit header
    ctx = resolve(from_env=True)
    assert ctx.trace_id == "4bf92f3577b34da6a3ce929d0e0e4736"
    assert ctx.service == "from-env"
    # explicit header wins, and an explicit service overrides
    other = "00-11111111111111111111111111111111-2222222222222222-01"
    ctx2 = resolve(traceparent=other, service="explicit")
    assert ctx2.trace_id == "1" * 32
    assert ctx2.service == "explicit"


def test_new_root_is_valid_and_unique():
    a = TraceContext.new_root(service="svc")
    b = TraceContext.new_root(service="svc")
    assert a.trace_id != b.trace_id
    assert TraceContext.parse(a.traceparent()) is not None


def test_correlation_is_stamped_on_a_crash(tmp_path):
    flight.install(output_dir=tmp_path)
    flight.correlate(VALID_TP, service="checkout")
    flight.link("svcA-crash.flight", service="gateway")
    try:
        {}["missing"]
    except KeyError:
        path = flight.capture()
    flight.uninstall()

    f = flight.read(path)
    ctx = f.correlation()
    assert ctx is not None
    assert ctx.trace_id == "4bf92f3577b34da6a3ce929d0e0e4736"
    assert ctx.service == "checkout"
    assert f.trace_id == ctx.trace_id
    assert [(l.ref, l.service) for l in ctx.links] == [("svcA-crash.flight", "gateway")]
    # and the crash detail is still fully present alongside the correlation
    assert f.has_crash
    assert f.exceptions[0][0] == "KeyError"


def test_trace_graph_groups_two_services(tmp_path):
    """Two black boxes sharing a trace id form one cross-service group."""

    def crash_with(service, out):
        flight.install(output_dir=tmp_path)
        flight.correlate(VALID_TP, service=service)
        try:
            {}["x"]
        except KeyError:
            p = flight.capture(path=str(out))
        flight.uninstall()
        return p

    a = crash_with("gateway", tmp_path / "a.flight")
    b = crash_with("checkout", tmp_path / "b.flight")
    graph = trace_graph([flight.read(a), flight.read(b)])
    assert list(graph.keys()) == ["4bf92f3577b34da6a3ce929d0e0e4736"]
    services = sorted(n.service for n in graph["4bf92f3577b34da6a3ce929d0e0e4736"])
    assert services == ["checkout", "gateway"]


def test_uncorrelated_flight_has_no_context(tmp_path):
    flight.install(output_dir=tmp_path)
    try:
        {}["x"]
    except KeyError:
        p = flight.capture()
    flight.uninstall()
    assert flight.read(p).correlation() is None


# =========================================================================
# Adaptive overhead governor
# =========================================================================


def test_estimate_overhead_arithmetic():
    # 1M events/s at 65 ns each ≈ 6.5% of one core.
    assert estimate_overhead(1_000_000, 1.0, 65.0) == pytest.approx(0.065)
    assert estimate_overhead(0, 1.0, 65.0) == 0.0
    assert estimate_overhead(1000, 0.0, 65.0) == 0.0  # guard divide-by-zero


def test_ladder_demotes_under_pressure_then_promotes_when_quiet():
    lad = OverheadLadder(
        baseline=LEVEL_LINES, ceiling=0.03, demote_after=2, promote_after=3, promote_ratio=0.5
    )
    # Two hot samples per rung → step down LINES → RETURNS → CALLS.
    assert lad.observe(0.10) == LEVEL_LINES  # first over
    assert lad.observe(0.10) == LEVEL_RETURNS  # demote
    assert lad.observe(0.10) == LEVEL_RETURNS
    assert lad.observe(0.10) == LEVEL_CALLS  # demote again
    # Never below the floor.
    assert lad.observe(0.10) == LEVEL_CALLS
    assert lad.observe(0.10) == LEVEL_CALLS
    # Quiet → climb back, capped at the baseline.
    for _ in range(3):
        lad.observe(0.001)
    assert lad.level == LEVEL_RETURNS
    for _ in range(3):
        lad.observe(0.001)
    assert lad.level == LEVEL_LINES
    for _ in range(3):
        lad.observe(0.001)
    assert lad.level == LEVEL_LINES  # capped


def test_ladder_holds_in_the_comfortable_band():
    lad = OverheadLadder(baseline=LEVEL_LINES, ceiling=0.03, demote_after=2)
    # Just under the ceiling but above promote threshold → no move, no streak.
    for _ in range(10):
        assert lad.observe(0.025) == LEVEL_LINES


def test_ladder_never_promotes_above_baseline():
    lad = OverheadLadder(baseline=LEVEL_CALLS, ceiling=0.03, promote_after=1)
    for _ in range(5):
        lad.observe(0.0)
    assert lad.level == LEVEL_CALLS


def test_governor_tick_demotes_with_injected_clock_and_stats():
    applied: list[int] = []
    t = {"v": 0.0}
    ev = {"v": 0}
    gov = Governor(
        baseline=LEVEL_LINES,
        ceiling=0.03,
        per_event_ns=65.0,
        stats_source=lambda: {"total_events": ev["v"]},
        apply=applied.append,
        clock=lambda: t["v"],
    )
    # First tick just establishes the baseline (no delta yet).
    gov.tick()
    assert applied == []
    # Then hammer it: 1M events per 1 s tick → ~6.5% → over the ceiling.
    for _ in range(3):
        t["v"] += 1.0
        ev["v"] += 1_000_000
        gov.tick()
    assert applied == [LEVEL_RETURNS]  # demoted once (hysteresis)
    assert gov.last_estimate == pytest.approx(0.065)


def test_governor_promotes_back_when_idle():
    applied: list[int] = []
    t = {"v": 0.0}
    ev = {"v": 0}
    gov = Governor(
        baseline=LEVEL_LINES,
        stats_source=lambda: {"total_events": ev["v"]},
        apply=applied.append,
        clock=lambda: t["v"],
    )
    gov.ladder.reset(LEVEL_CALLS)  # pretend we were demoted
    gov.tick()  # establish baseline sample
    # Two rungs to climb at promote_after=4 → needs 8 quiet observations.
    for _ in range(8):  # quiet: no new events
        t["v"] += 1.0
        gov.tick()
    assert gov.ladder.level == LEVEL_LINES
    assert applied[-1] == LEVEL_LINES


def test_governor_retunes_a_live_session_without_error():
    flight.install(record_lines=True)
    sess = flight._install._active
    assert sess.baseline_level == LEVEL_LINES
    sess.set_ring_level(LEVEL_CALLS)  # demote live
    sess.set_ring_level(999)  # capped to baseline, no crash
    flight.uninstall()


def test_install_with_slo_starts_and_stops_governor():
    flight.install(overhead_slo=0.03)
    sess = flight._install._active
    assert sess._governor is not None
    flight.uninstall()  # must join the governor thread cleanly


# =========================================================================
# Crash-surviving supervisor daemon
# =========================================================================


def test_promote_renames_checkpoint(tmp_path):
    from flight._daemon import _promote

    ckpt = tmp_path / "ckpt.flight"
    ckpt.write_bytes(b"FLGT-not-really-but-enough-to-move")
    final = tmp_path / "final.flight"
    out = _promote(ckpt, final)
    assert out == final
    assert final.exists()
    assert not ckpt.exists()


def test_promote_without_checkpoint_is_noop(tmp_path):
    from flight._daemon import _promote

    assert _promote(tmp_path / "nope.flight", tmp_path / "final.flight") is None


def test_supervise_clean_shutdown_drops_checkpoint(tmp_path):
    """When the parent announces a clean shutdown, nothing is promoted and the
    checkpoint is discarded."""
    from flight._daemon import _CLEAN, supervise

    ckpt = tmp_path / "ckpt.flight"
    ckpt.write_bytes(b"checkpoint")
    r, w = os.pipe()
    os.write(w, _CLEAN)
    os.close(w)  # EOF after the clean byte
    result = supervise(r, ckpt, tmp_path, os.getpid())
    assert result is None
    assert not ckpt.exists()  # discarded
    assert not list(tmp_path.glob("flight-killed-*.flight"))


def test_supervise_promotes_on_unclean_death(tmp_path):
    """When the pipe reaches EOF *without* the clean byte (an uncatchable
    death), the last checkpoint becomes the black box."""
    from flight._daemon import supervise

    ckpt = tmp_path / "ckpt.flight"
    ckpt.write_bytes(b"checkpoint-bytes")
    r, w = os.pipe()
    os.close(w)  # EOF, no clean byte → treated as a crash
    result = supervise(r, ckpt, tmp_path, 4242)
    assert result is not None
    assert result.exists()
    assert result.name.startswith("flight-killed-4242-")
    assert not ckpt.exists()


# --- the real thing: SIGKILL a child and recover its black box -----------

_CHILD = textwrap.dedent(
    """
    import sys, time
    sys.path.insert(0, {pkg!r})
    from pathlib import Path
    import flight
    flight.install(output_dir=Path({out!r}))
    flight.start_daemon(daemon_interval=0.1)

    def work(n):
        return sum(i * i for i in range(n))

    for _ in range(40):
        work(200)
        time.sleep(0.01)
    print("READY", flush=True)
    time.sleep(60)   # wait to be killed
    """
)


@pytest.mark.skipif(not hasattr(signal, "SIGKILL"), reason="needs SIGKILL")
def test_black_box_survives_sigkill(tmp_path):
    pkg = str(Path(flight.__file__).resolve().parent.parent)
    script = tmp_path / "child.py"
    script.write_text(_CHILD.format(pkg=pkg, out=str(tmp_path)))

    proc = subprocess.Popen(
        [sys.executable, str(script)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        # Wait for the child to have recorded some events and checkpointed.
        deadline = time.time() + 10
        line = ""
        while time.time() < deadline:
            line = proc.stdout.readline()
            if "READY" in line:
                break
        assert "READY" in line, f"child never got ready (got {line!r})"
        time.sleep(0.25)  # let at least one checkpoint land
        proc.send_signal(signal.SIGKILL)  # the plane goes down, no Python runs
    finally:
        proc.wait(timeout=10)

    # The supervisor (a separate process) should now flush the black box.
    deadline = time.time() + 10
    killed: list[Path] = []
    while time.time() < deadline:
        killed = list(tmp_path.glob("flight-killed-*.flight"))
        if killed:
            break
        time.sleep(0.1)
    assert killed, "no black box was recovered after SIGKILL"

    f = flight.read(killed[0])
    assert not f.partial
    assert f.event_count > 0  # the rear-view mirror survived the crash
    assert "EVENT_RING" in f.blocks


_CLEAN_CHILD = textwrap.dedent(
    """
    import sys
    sys.path.insert(0, {pkg!r})
    from pathlib import Path
    import flight
    flight.install(output_dir=Path({out!r}))
    flight.start_daemon(daemon_interval=0.05)
    def work(n):
        return sum(range(n))
    for _ in range(10):
        work(500)
    print("done", flush=True)
    # Fall off the end: a *clean* exit. atexit must announce it so the
    # supervisor discards the checkpoint instead of promoting it.
    """
)


def test_clean_exit_leaves_no_killed_file(tmp_path):
    """A daemon process that exits cleanly must not leave a `flight-killed-*`
    black box behind (the atexit clean-shutdown handshake)."""
    pkg = str(Path(flight.__file__).resolve().parent.parent)
    script = tmp_path / "clean.py"
    script.write_text(_CLEAN_CHILD.format(pkg=pkg, out=str(tmp_path)))
    proc = subprocess.run(
        [sys.executable, str(script)], capture_output=True, text=True, timeout=30
    )
    assert proc.returncode == 0, proc.stderr
    time.sleep(0.4)  # give the supervisor time to (not) promote
    assert not list(tmp_path.glob("flight-killed-*.flight"))
    assert not list(tmp_path.glob(".flight-ckpt-*"))  # checkpoint discarded
