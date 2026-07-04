"""Exhaustive Phase 8/9 tests — distributed correlation, the overhead governor,
the crash-surviving supervisor daemon, and at-rest encryption.

Everything here is deterministic: the ladder/governor are driven with injected
clocks and stats sources, the daemon is exercised over real ``os.pipe``s and a
short-lived subprocess with tight timeouts, and the AEAD round-trips are guarded
with ``skipif`` so they skip cleanly when the optional ``cryptography`` package
is absent (the stdlib KDF/framing paths always run).
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

import flight
from flight import _crypto
from flight._config import Config
from flight._correlation import (
    Link,
    TraceContext,
    resolve,
    trace_graph,
)
from flight._daemon import _CLEAN, _final_path, _promote, supervise
from flight._governor import (
    LEVEL_CALLS,
    LEVEL_LINES,
    LEVEL_RETURNS,
    Governor,
    OverheadLadder,
    estimate_overhead,
)

# =========================================================================
# Helpers
# =========================================================================

_TID = "4bf92f3577b34da6a3ce929d0e0e4736"
_SID = "00f067aa0ba902b7"


def _tp(trace_id=_TID, span_id=_SID, flags="01", version="00"):
    return f"{version}-{trace_id}-{span_id}-{flags}"


class _FakeFlight:
    """Minimal stand-in for a parsed Flight in trace_graph()."""

    def __init__(self, path, ctx):
        self.path = path
        self._ctx = ctx

    def correlation(self):
        if isinstance(self._ctx, Exception):
            raise self._ctx
        return self._ctx


# =========================================================================
# TraceContext.parse — valid traceparents round-trip
# =========================================================================

# All version "00", lowercase, 2-hex flags → traceparent() must reproduce input.
_VALID_TPS = [
    _tp(),
    _tp(flags="00"),
    _tp(flags="ff"),
    _tp(flags="03"),
    _tp("11111111111111111111111111111111", "2222222222222222", "01"),
    _tp("00000000000000000000000000000001", "0000000000000001", "01"),
    _tp("abcdefabcdefabcdefabcdefabcdefab", "abcdefabcdefabcd", "02"),
    _tp("0123456789abcdef0123456789abcdef", "0123456789abcdef", "00"),
    _tp("ffffffffffffffffffffffffffffffff", "ffffffffffffffff", "ff"),
    _tp("deadbeefdeadbeefdeadbeefdeadbeef", "cafebabecafebabe", "01"),
    _tp(flags="7f"),
    _tp(flags="80"),
    _tp(flags="7e"),
    _tp("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "bbbbbbbbbbbbbbbb", "05"),
    _tp("1234567890abcdef1234567890abcdef", "fedcba0987654321", "09"),
    _tp("0f0f0f0f0f0f0f0f0f0f0f0f0f0f0f0f", "0f0f0f0f0f0f0f0f", "01"),
]


@pytest.mark.parametrize("tp", _VALID_TPS)
def test_valid_traceparent_round_trips(tp):
    ctx = TraceContext.parse(tp)
    assert ctx is not None
    # Fields agree with the source header.
    _v, tid, sid, fl = tp.split("-")
    assert ctx.trace_id == tid
    assert ctx.span_id == sid
    assert ctx.flags == int(fl, 16) & 0xFF
    # And it renders back byte-for-byte (version pinned to 00, lowercase).
    assert ctx.traceparent() == tp


@pytest.mark.parametrize(
    "raw,lowered",
    [
        (_tp("4BF92F3577B34DA6A3CE929D0E0E4736", "00F067AA0BA902B7", "01"), _tp()),
        (_tp("ABCDEFABCDEFABCDEFABCDEFABCDEFAB", "ABCDEFABCDEFABCD", "0A"),
         _tp("abcdefabcdefabcdefabcdefabcdefab", "abcdefabcdefabcd", "0a")),
        ("  " + _tp() + "  ", _tp()),  # surrounding whitespace is stripped
    ],
)
def test_traceparent_is_normalised(raw, lowered):
    ctx = TraceContext.parse(raw)
    assert ctx is not None
    assert ctx.traceparent() == lowered


# =========================================================================
# TraceContext.parse — malformed headers reject to None
# =========================================================================

_BAD_TPS = [
    "",
    "   ",  # blank -> splits to 1 field
    "garbage",
    "00",
    "00-" + _TID,
    "00-" + _TID + "-" + _SID,  # missing flags field (3 parts)
    "-".join(["00", _TID, _SID, "01", "extra"]),  # 5 parts
    _tp(trace_id="tooShort"),
    _tp(trace_id=_TID + "ab"),  # 34 hex trace id
    _tp(trace_id=_TID[:-1]),  # 31 hex trace id
    _tp(span_id="short"),
    _tp(span_id=_SID + "ff"),  # 18 hex span id
    _tp(trace_id="zzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzz"),  # non-hex trace
    _tp(span_id="zzzzzzzzzzzzzzzz"),  # non-hex span
    _tp(trace_id="0" * 32),  # all-zero trace id
    _tp(span_id="0" * 16),  # all-zero span id
    _tp(version="0"),  # 1-char version
    _tp(version="000"),  # 3-char version
    _tp(version="zz"),  # non-hex version
    _tp(flags="zz"),  # non-hex flags
    _tp(flags=""),  # empty flags -> int("",16) ValueError
    _tp(flags="g1"),  # non-hex flags
    _tp(trace_id="4bf92f3577b34da6a3ce929d0e0e473 "),  # embedded space
]


@pytest.mark.parametrize("bad", _BAD_TPS)
def test_malformed_traceparent_rejected(bad):
    assert TraceContext.parse(bad) is None


# =========================================================================
# sampled flag bit
# =========================================================================


@pytest.mark.parametrize(
    "flag_hex,expected",
    [
        ("00", False), ("01", True), ("02", False), ("03", True),
        ("04", False), ("05", True), ("0e", False), ("0f", True),
        ("10", False), ("11", True), ("7e", False), ("7f", True),
        ("80", False), ("81", True), ("fe", False), ("ff", True),
    ],
)
def test_sampled_flag_bit(flag_hex, expected):
    ctx = TraceContext.parse(_tp(flags=flag_hex))
    assert ctx is not None
    assert ctx.flags == int(flag_hex, 16)
    assert ctx.sampled is expected


# =========================================================================
# from_env
# =========================================================================


@pytest.mark.parametrize(
    "env,expect_tid,expect_service,expect_state",
    [
        ({}, None, None, None),
        ({"TRACEPARENT": _tp()}, _TID, None, ""),
        ({"TRACEPARENT": _tp(), "OTEL_SERVICE_NAME": "svc"}, _TID, "svc", ""),
        ({"TRACEPARENT": _tp(), "FLIGHT_SERVICE": "fs"}, _TID, "fs", ""),
        # OTEL_SERVICE_NAME wins over FLIGHT_SERVICE.
        ({"TRACEPARENT": _tp(), "OTEL_SERVICE_NAME": "o", "FLIGHT_SERVICE": "f"}, _TID, "o", ""),
        ({"TRACEPARENT": _tp(), "TRACESTATE": "a=b,c=d"}, _TID, None, "a=b,c=d"),
        ({"TRACEPARENT": "garbage"}, None, None, None),
        ({"TRACESTATE": "a=b"}, None, None, None),  # no traceparent
        ({"TRACEPARENT": "", "OTEL_SERVICE_NAME": "x"}, None, None, None),  # empty tp
    ],
)
def test_from_env(env, expect_tid, expect_service, expect_state):
    ctx = TraceContext.from_env(environ=env)
    if expect_tid is None:
        assert ctx is None
    else:
        assert ctx is not None
        assert ctx.trace_id == expect_tid
        assert ctx.service == expect_service
        assert ctx.trace_state == expect_state


# =========================================================================
# new_root — validity & uniqueness
# =========================================================================


@pytest.mark.parametrize("i", range(12))
def test_new_root_is_valid(i):
    ctx = TraceContext.new_root(service="svc")
    assert ctx.service == "svc"
    assert ctx.flags == 1 and ctx.sampled
    # A fresh root must be a parseable, non-zero traceparent.
    assert TraceContext.parse(ctx.traceparent()) is not None
    assert ctx.trace_id != "0" * 32
    assert ctx.span_id != "0" * 16


def test_new_root_ids_are_unique():
    roots = [TraceContext.new_root() for _ in range(64)]
    assert len({r.trace_id for r in roots}) == 64
    assert len({r.span_id for r in roots}) == 64


# =========================================================================
# with_link / with_service immutability
# =========================================================================


@pytest.mark.parametrize(
    "service", ["checkout", "gateway", "billing-svc", "a", "svc-with-dashes"]
)
def test_with_service_is_immutable(service):
    base = TraceContext.parse(_tp())
    new = base.with_service(service)
    assert base.service is None  # original untouched
    assert new.service == service
    # everything else identical
    assert new.trace_id == base.trace_id
    assert new.span_id == base.span_id
    assert new.flags == base.flags
    assert new.links == base.links


@pytest.mark.parametrize("n", [1, 2, 3, 5])
def test_with_link_is_immutable_and_accumulates(n):
    base = TraceContext.parse(_tp())
    ctx = base
    for i in range(n):
        ctx = ctx.with_link(Link(_TID, f"svc{i}.flight", f"svc{i}"))
    assert base.links == ()  # original untouched
    assert len(ctx.links) == n
    assert [l.ref for l in ctx.links] == [f"svc{i}.flight" for i in range(n)]


@pytest.mark.parametrize(
    "ref,service,expected_render",
    [
        ("svcA.flight", "gateway", "svcA.flight [gateway]"),
        ("svcB.flight", None, "svcB.flight"),
        ("http://x/y", "edge", "http://x/y [edge]"),
        ("span-123", "", "span-123"),  # falsy service → no bracket
    ],
)
def test_link_render(ref, service, expected_render):
    assert Link(_TID, ref, service).render() == expected_render


# =========================================================================
# to_nondet / from_nondet round-trip
# =========================================================================


@pytest.mark.parametrize("service", [None, "checkout", "gateway"])
@pytest.mark.parametrize("state", ["", "a=b", "vendor=v1,other=2"])
@pytest.mark.parametrize("nlinks", [0, 1, 3])
def test_nondet_round_trip(service, state, nlinks):
    ctx = TraceContext.parse(_tp(), trace_state=state, service=service)
    for i in range(nlinks):
        svc = f"up{i}" if i % 2 == 0 else None
        ctx = ctx.with_link(Link(_TID, f"up{i}.flight", svc))
    rows = ctx.to_nondet()
    back = TraceContext.from_nondet(rows)
    assert back is not None
    assert back.trace_id == ctx.trace_id
    assert back.span_id == ctx.span_id
    assert back.flags == ctx.flags
    assert back.trace_state == state
    assert back.service == service
    assert len(back.links) == nlinks
    assert [(l.ref, l.service) for l in back.links] == [
        (l.ref, l.service) for l in ctx.links
    ]


@pytest.mark.parametrize(
    "rows",
    [
        [],
        [(0, "time.time", "f", "1.0")],
        [(0, "random.random", "r", "0.5"), (1, "os.urandom", "r", "ff")],
        [("bad", "row")],  # malformed row: no traceparent source
    ],
)
def test_from_nondet_none_without_context(rows):
    assert TraceContext.from_nondet(rows) is None


def test_from_nondet_ignores_unparseable_rows_but_keeps_context():
    rows = [
        ("junk",),  # too short — skipped
        (0, "otel.traceparent", "w", _tp()),
        (2, "otel.service", "w", "svc"),
    ]
    ctx = TraceContext.from_nondet(rows)
    assert ctx is not None
    assert ctx.trace_id == _TID
    assert ctx.service == "svc"


# =========================================================================
# resolve — precedence
# =========================================================================


@pytest.mark.parametrize(
    "explicit,env_tp,service,from_env,expect_tid,expect_service",
    [
        # explicit wins, service overrides
        (_tp("1" * 32, "2" * 16), _tp(), "explicit", True, "1" * 32, "explicit"),
        # explicit invalid → falls back to env
        ("garbage", _tp(), None, True, _TID, None),
        # no explicit, env used
        (None, _tp(), None, True, _TID, None),
        # no explicit, env disabled → None
        (None, _tp(), None, False, None, None),
        # env with service override applied via with_service
        (None, _tp(), "override", True, _TID, "override"),
        # nothing available at all
        (None, None, None, True, None, None),
    ],
)
def test_resolve_precedence(monkeypatch, explicit, env_tp, service, from_env, expect_tid, expect_service):
    monkeypatch.delenv("TRACEPARENT", raising=False)
    monkeypatch.delenv("OTEL_SERVICE_NAME", raising=False)
    monkeypatch.delenv("FLIGHT_SERVICE", raising=False)
    if env_tp is not None:
        monkeypatch.setenv("TRACEPARENT", env_tp)
    ctx = resolve(traceparent=explicit, service=service, from_env=from_env)
    if expect_tid is None:
        assert ctx is None
    else:
        assert ctx is not None
        assert ctx.trace_id == expect_tid
        assert ctx.service == expect_service


# =========================================================================
# trace_graph
# =========================================================================


def test_trace_graph_groups_by_trace_id():
    tid_a = "1" * 32
    tid_b = "2" * 32
    ca = TraceContext.parse(_tp(tid_a, "1" * 16)).with_service("gateway")
    cb = TraceContext.parse(_tp(tid_a, "2" * 16)).with_service("checkout")
    cc = TraceContext.parse(_tp(tid_b, "3" * 16)).with_service("billing")
    flights = [
        _FakeFlight("a.flight", ca),
        _FakeFlight("b.flight", cb),
        _FakeFlight("c.flight", cc),
    ]
    graph = trace_graph(flights)
    assert set(graph) == {tid_a, tid_b}
    assert sorted(n.service for n in graph[tid_a]) == ["checkout", "gateway"]
    assert [n.path for n in graph[tid_b]] == ["c.flight"]


@pytest.mark.parametrize("bad_ctx", [None, RuntimeError("boom")])
def test_trace_graph_skips_uncorrelated_and_raising(bad_ctx):
    good = _FakeFlight("good.flight", TraceContext.parse(_tp()))
    bad = _FakeFlight("bad.flight", bad_ctx)
    graph = trace_graph([good, bad])
    # only the good one is grouped; the bad one never raises out
    assert list(graph) == [_TID]
    assert [n.path for n in graph[_TID]] == ["good.flight"]


def test_trace_node_service_defaults_to_question_mark():
    ctx = TraceContext.parse(_tp())  # no service
    graph = trace_graph([_FakeFlight("x.flight", ctx)])
    assert graph[_TID][0].service == "?"


# =========================================================================
# estimate_overhead — arithmetic
# =========================================================================


@pytest.mark.parametrize(
    "events,elapsed,per_ns,expected",
    [
        (1_000_000, 1.0, 65.0, 0.065),
        (2_000_000, 1.0, 65.0, 0.130),
        (1_000_000, 2.0, 65.0, 0.0325),
        (0, 1.0, 65.0, 0.0),
        (1000, 0.0, 65.0, 0.0),       # divide-by-zero guard
        (1000, -1.0, 65.0, 0.0),      # negative elapsed guard
        (500_000, 1.0, 100.0, 0.05),
        (1, 1.0, 1e9, 1.0),           # one event costing a full second
        (100, 0.001, 1000.0, 0.1),
        (10_000_000, 1.0, 65.0, 0.65),
    ],
)
def test_estimate_overhead(events, elapsed, per_ns, expected):
    assert estimate_overhead(events, elapsed, per_ns) == pytest.approx(expected)


# =========================================================================
# OverheadLadder — state-machine sequences
# =========================================================================


@pytest.mark.parametrize(
    "kwargs,start_level,estimates,expected_levels",
    [
        # demote LINES→RETURNS→CALLS at demote_after=2, then hold at floor.
        (dict(baseline=LEVEL_LINES, ceiling=0.03, demote_after=2, floor=LEVEL_CALLS),
         None, [0.1] * 6, [2, 1, 1, 0, 0, 0]),
        # promote from CALLS→LINES at promote_after=3 (ratio 0.5 → threshold .015).
        (dict(baseline=LEVEL_LINES, ceiling=0.03, promote_after=3, promote_ratio=0.5),
         LEVEL_CALLS, [0.001] * 9, [0, 0, 1, 1, 1, 2, 2, 2, 2]),
        # in-band holds forever, no move.
        (dict(baseline=LEVEL_LINES, ceiling=0.03, demote_after=2, promote_ratio=0.5),
         None, [0.025] * 5, [2, 2, 2, 2, 2]),
        # alternating over / in-band: streak resets, never demotes.
        (dict(baseline=LEVEL_LINES, ceiling=0.03, demote_after=2),
         None, [0.1, 0.025, 0.1, 0.025], [2, 2, 2, 2]),
        # floor=RETURNS: never drops below it.
        (dict(baseline=LEVEL_LINES, ceiling=0.03, demote_after=1, floor=LEVEL_RETURNS),
         None, [0.1] * 5, [1, 1, 1, 1, 1]),
        # baseline=CALLS: can never promote above baseline even when idle.
        (dict(baseline=LEVEL_CALLS, ceiling=0.03, promote_after=1),
         None, [0.0] * 4, [0, 0, 0, 0]),
        # single spike never demotes (needs demote_after=2 in a row).
        (dict(baseline=LEVEL_LINES, ceiling=0.03, demote_after=2, promote_ratio=0.5),
         None, [0.1, 0.02, 0.02], [2, 2, 2]),
    ],
)
def test_ladder_sequences(kwargs, start_level, estimates, expected_levels):
    lad = OverheadLadder(**kwargs)
    if start_level is not None:
        lad.reset(start_level)
    got = [lad.observe(e) for e in estimates]
    assert got == expected_levels
    # invariant: always within [floor, baseline]
    floor = kwargs.get("floor", LEVEL_CALLS)
    assert all(floor <= lvl <= kwargs["baseline"] for lvl in got)


@pytest.mark.parametrize("target", [LEVEL_CALLS, LEVEL_RETURNS, LEVEL_LINES, None])
def test_ladder_reset(target):
    lad = OverheadLadder(baseline=LEVEL_LINES, demote_after=1)
    lad.observe(0.5)  # demote to RETURNS
    assert lad.level == LEVEL_RETURNS
    lad.reset(target)
    assert lad.level == (LEVEL_LINES if target is None else target)
    # internal streaks cleared: a single over-sample won't immediately demote
    # (demote_after=1 would, so use a fresh multi-step check on counters)


def test_ladder_streak_cleared_by_reset():
    lad = OverheadLadder(baseline=LEVEL_LINES, demote_after=2)
    lad.observe(0.5)  # over=1
    lad.reset()       # clears the streak
    assert lad.observe(0.5) == LEVEL_LINES  # only over=1 again, no demote


# =========================================================================
# Governor.tick — injected clock/stats
# =========================================================================


def _make_gov(**kw):
    state = {"t": 0.0, "ev": 0, "applied": [], "changes": []}
    gov = Governor(
        stats_source=lambda: {"total_events": state["ev"]},
        apply=state["applied"].append,
        clock=lambda: state["t"],
        on_change=lambda p, n, est: state["changes"].append((p, n)),
        **kw,
    )
    return gov, state


def test_governor_first_tick_is_baseline_only():
    gov, st = _make_gov(baseline=LEVEL_LINES)
    assert gov.tick() == LEVEL_LINES
    assert st["applied"] == []  # no change on the very first sample
    assert gov.last_estimate == 0.0


def test_governor_demotes_under_load():
    gov, st = _make_gov(baseline=LEVEL_LINES, ceiling=0.03, per_event_ns=65.0)
    gov.tick()  # baseline
    for _ in range(3):
        st["t"] += 1.0
        st["ev"] += 1_000_000  # ~6.5% overhead → over the 3% ceiling
        gov.tick()
    assert st["applied"] == [LEVEL_RETURNS]  # one demote (hysteresis, demote_after=2)
    assert st["changes"] == [(LEVEL_LINES, LEVEL_RETURNS)]
    assert gov.last_estimate == pytest.approx(0.065)


def test_governor_promotes_when_idle():
    gov, st = _make_gov(baseline=LEVEL_LINES)
    gov.ladder.reset(LEVEL_CALLS)  # pretend previously demoted
    gov.tick()  # baseline sample
    for _ in range(8):  # promote_after defaults to 4 → 2 rungs need 8 quiet samples
        st["t"] += 1.0  # no new events → 0 overhead
        gov.tick()
    assert gov.ladder.level == LEVEL_LINES
    assert st["applied"][-1] == LEVEL_LINES
    # climbed exactly two rungs
    assert st["changes"] == [(LEVEL_CALLS, LEVEL_RETURNS), (LEVEL_RETURNS, LEVEL_LINES)]


def test_governor_read_events_survives_bad_stats_source():
    gov = Governor(
        baseline=LEVEL_LINES,
        stats_source=lambda: (_ for _ in ()).throw(RuntimeError("nope")),
        apply=lambda lvl: None,
        clock=lambda: 0.0,
    )
    # tick must not raise even when the stats source explodes
    assert gov.tick() == LEVEL_LINES


def test_governor_start_stop_thread_lifecycle():
    # Deterministic: a stats source that never breaches the ceiling and a fast
    # interval so the thread joins quickly. apply captures the restore call.
    applied = []
    gov = Governor(
        baseline=LEVEL_LINES,
        interval=0.01,
        stats_source=lambda: {"total_events": 0},
        apply=applied.append,
        clock=lambda: time.monotonic(),
    )
    gov.start()
    assert gov._thread is not None and gov._thread.is_alive()
    time.sleep(0.05)
    gov.stop()  # sets stop event, joins, restores baseline
    assert gov._thread is None
    # stop() restores the user's baseline granularity on the way out
    assert applied and applied[-1] == LEVEL_LINES
    assert gov.ladder.level == LEVEL_LINES


def test_governor_start_is_idempotent():
    gov = Governor(baseline=LEVEL_CALLS, interval=0.01,
                   stats_source=lambda: {"total_events": 0},
                   apply=lambda l: None, clock=time.monotonic)
    gov.start()
    t1 = gov._thread
    gov.start()  # second start is a no-op
    assert gov._thread is t1
    gov.stop()


# =========================================================================
# Daemon — _promote / _final_path / supervise
# =========================================================================


def test_promote_renames_existing_checkpoint(tmp_path):
    ckpt = tmp_path / "ckpt.flight"
    ckpt.write_bytes(b"payload-bytes")
    final = tmp_path / "sub" / "final.flight"  # parent created on demand
    out = _promote(ckpt, final)
    assert out == final
    assert final.exists()
    assert final.read_bytes() == b"payload-bytes"
    assert not ckpt.exists()


def test_promote_missing_checkpoint_is_none(tmp_path):
    assert _promote(tmp_path / "nope.flight", tmp_path / "final.flight") is None


@pytest.mark.parametrize("pid", [1, 42, 4242, 999999])
def test_final_path_format(tmp_path, pid):
    p = _final_path(tmp_path, pid)
    assert p.parent == tmp_path
    assert p.name.startswith(f"flight-killed-{pid}-")
    assert p.suffix == ".flight"
    # trailing component before .flight is an int millisecond stamp
    stamp = p.stem.rsplit("-", 1)[1]
    assert stamp.isdigit()


@pytest.mark.parametrize("trailing_bytes", [b"", b"more", b"CCC"])
def test_supervise_clean_shutdown_discards(tmp_path, trailing_bytes):
    ckpt = tmp_path / "ckpt.flight"
    ckpt.write_bytes(b"checkpoint")
    r, w = os.pipe()
    os.write(w, _CLEAN + trailing_bytes)
    os.close(w)  # EOF after the clean byte
    result = supervise(r, ckpt, tmp_path, os.getpid())
    assert result is None
    assert not ckpt.exists()  # discarded
    assert not list(tmp_path.glob("flight-killed-*.flight"))


@pytest.mark.parametrize("pid", [7, 4242, 31337])
def test_supervise_promotes_on_unclean_death(tmp_path, pid):
    ckpt = tmp_path / "ckpt.flight"
    ckpt.write_bytes(b"checkpoint-bytes")
    r, w = os.pipe()
    os.close(w)  # EOF, no clean byte → treated as a crash
    result = supervise(r, ckpt, tmp_path, pid)
    assert result is not None and result.exists()
    assert result.name.startswith(f"flight-killed-{pid}-")
    assert result.read_bytes() == b"checkpoint-bytes"
    assert not ckpt.exists()


def test_supervise_unclean_death_without_checkpoint_returns_none(tmp_path):
    r, w = os.pipe()
    os.close(w)
    # No checkpoint file on disk → nothing to promote.
    result = supervise(r, tmp_path / "missing.flight", tmp_path, 123)
    assert result is None
    assert not list(tmp_path.glob("flight-killed-*.flight"))


def test_supervise_clean_byte_mid_stream(tmp_path):
    """The clean byte can arrive after other bytes; it still counts as clean."""
    ckpt = tmp_path / "ckpt.flight"
    ckpt.write_bytes(b"x")
    r, w = os.pipe()
    os.write(w, b"noise")
    os.write(w, _CLEAN)
    os.close(w)
    assert supervise(r, ckpt, tmp_path, os.getpid()) is None
    assert not ckpt.exists()


# =========================================================================
# Daemon — clean start/stop handshake leaves no killed file
# =========================================================================


def test_daemon_clean_stop_leaves_no_killed_file(tmp_path):
    cfg = Config(output_dir=tmp_path)
    d = flight._daemon.Daemon(cfg, interval=0.05)
    d.start()
    try:
        assert d._started
        assert d._proc is not None
        assert d._write_fd is not None
        time.sleep(0.1)  # let a checkpoint or two land
    finally:
        d.stop(clean=True)  # announce a clean shutdown
    assert not d._started
    # The supervisor must NOT have promoted a killed file on a clean shutdown.
    # Give it a brief window to exit and clean up.
    deadline = time.time() + 3
    while time.time() < deadline and list(tmp_path.glob("flight-killed-*.flight")):
        time.sleep(0.05)
    assert not list(tmp_path.glob("flight-killed-*.flight"))
    # And the checkpoint was discarded by the supervisor.
    deadline = time.time() + 3
    while time.time() < deadline and d.checkpoint.exists():
        time.sleep(0.05)
    assert not d.checkpoint.exists()


def test_daemon_stop_before_start_is_noop(tmp_path):
    cfg = Config(output_dir=tmp_path)
    d = flight._daemon.Daemon(cfg, interval=0.05)
    d.stop(clean=True)  # never started → must not raise
    assert not d._started


# --- real SIGKILL end-to-end (focused, kept reliable) --------------------

_CHILD = """
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
time.sleep(60)
"""


@pytest.mark.skipif(not hasattr(signal, "SIGKILL"), reason="needs SIGKILL")
def test_black_box_survives_real_sigkill(tmp_path):
    pkg = str(Path(flight.__file__).resolve().parent.parent)
    script = tmp_path / "child.py"
    script.write_text(_CHILD.format(pkg=pkg, out=str(tmp_path)))
    proc = subprocess.Popen(
        [sys.executable, str(script)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    try:
        deadline = time.time() + 15
        line = ""
        while time.time() < deadline:
            line = proc.stdout.readline()
            if not line or "READY" in line:
                break
        assert "READY" in line, f"child never became ready (got {line!r})"
        time.sleep(0.3)  # let at least one checkpoint land
        proc.send_signal(signal.SIGKILL)
    finally:
        proc.wait(timeout=15)

    deadline = time.time() + 10
    killed = []
    while time.time() < deadline:
        killed = list(tmp_path.glob("flight-killed-*.flight"))
        if killed:
            break
        time.sleep(0.1)
    assert killed, "no black box recovered after SIGKILL"
    f = flight.read(killed[0])
    assert not f.partial
    assert f.event_count > 0


# =========================================================================
# Crypto — stdlib KDF & framing (always run)
# =========================================================================


@pytest.mark.parametrize(
    "passphrase,salt",
    [
        ("hunter2", b"\x00" * 16),
        ("hunter2", b"\x01" * 16),
        (b"raw-bytes-pass", b"salty-salt-16byt"),
        ("", b"z" * 16),
        ("unicode-🔑", b"s" * 16),
    ],
)
def test_derive_key_deterministic_and_32_bytes(passphrase, salt):
    k1 = _crypto.derive_key(passphrase, salt)
    k2 = _crypto.derive_key(passphrase, salt)
    assert k1 == k2  # deterministic
    assert isinstance(k1, bytes) and len(k1) == 32  # AES-256


@pytest.mark.parametrize(
    "p1,s1,p2,s2",
    [
        ("a", b"x" * 16, "b", b"x" * 16),        # different passphrase
        ("a", b"x" * 16, "a", b"y" * 16),        # different salt
        ("secret", b"1" * 16, "Secret", b"1" * 16),  # case-sensitive
    ],
)
def test_derive_key_sensitive_to_inputs(p1, s1, p2, s2):
    assert _crypto.derive_key(p1, s1) != _crypto.derive_key(p2, s2)


def test_str_and_bytes_passphrase_agree():
    salt = b"m" * 16
    assert _crypto.derive_key("abc", salt) == _crypto.derive_key(b"abc", salt)


@pytest.mark.parametrize(
    "salt,nonce,ct",
    [
        (b"S" * 16, b"N" * 12, b""),
        (b"S" * 16, b"N" * 12, b"cipher"),
        (bytes(range(16)), bytes(range(12)), b"\x00\xff\x10payload"),
        (os.urandom(16), os.urandom(12), os.urandom(64)),
    ],
)
def test_parse_envelope_round_trips_framing(salt, nonce, ct):
    blob = _crypto.MAGIC + salt + nonce + ct
    got_salt, got_nonce, got_ct = _crypto.parse_envelope(blob)
    assert got_salt == salt
    assert got_nonce == nonce
    assert got_ct == ct


@pytest.mark.parametrize(
    "blob",
    [
        b"",
        b"FLGT",                       # wrong magic, too short
        b"WRONGMAG" + b"S" * 16 + b"N" * 12,  # right length, wrong magic
        _crypto.MAGIC[:-1] + b"X" + b"S" * 16 + b"N" * 12,  # corrupted magic
        _crypto.MAGIC + b"tooshort",   # shorter than header
        _crypto.MAGIC + b"S" * 16 + b"N" * 11,  # nonce one byte short
    ],
)
def test_parse_envelope_rejects_bad_input(blob):
    with pytest.raises(_crypto.DecryptError):
        _crypto.parse_envelope(blob)


def test_parse_envelope_minimum_valid_header():
    # Exactly header length with empty ciphertext is valid.
    blob = _crypto.MAGIC + b"S" * 16 + b"N" * 12
    salt, nonce, ct = _crypto.parse_envelope(blob)
    assert salt == b"S" * 16 and nonce == b"N" * 12 and ct == b""


@pytest.mark.parametrize(
    "content,expected",
    [
        (_crypto.MAGIC, True),
        (_crypto.MAGIC + b"trailing", True),
        (b"FLGT-plain-flight", False),
        (b"", False),
        (_crypto.MAGIC[:-1], False),  # truncated magic
    ],
)
def test_looks_encrypted(tmp_path, content, expected):
    p = tmp_path / "f.bin"
    p.write_bytes(content)
    assert _crypto.looks_encrypted(p) is expected


def test_looks_encrypted_missing_file(tmp_path):
    assert _crypto.looks_encrypted(tmp_path / "does-not-exist") is False


# --- the AEAD path when cryptography is absent ---------------------------


@pytest.mark.skipif(_crypto.is_available(), reason="cryptography is installed")
@pytest.mark.parametrize("data", [b"", b"secret", b"\x00\xff" * 100])
def test_encrypt_bytes_raises_when_unavailable(data):
    with pytest.raises(_crypto.CryptoUnavailable):
        _crypto.encrypt_bytes(data, "pw")


@pytest.mark.skipif(_crypto.is_available(), reason="cryptography is installed")
def test_decrypt_bytes_raises_when_unavailable():
    # framing parses (stdlib), but the AEAD open needs cryptography
    blob = _crypto.MAGIC + b"S" * 16 + b"N" * 12 + b"ct"
    with pytest.raises(_crypto.CryptoUnavailable):
        _crypto.decrypt_bytes(blob, "pw")


@pytest.mark.skipif(_crypto.is_available(), reason="cryptography is installed")
def test_encrypt_file_raises_when_unavailable(tmp_path):
    src = tmp_path / "in.flight"
    src.write_bytes(b"data")
    with pytest.raises(_crypto.CryptoUnavailable):
        _crypto.encrypt_file(src, "pw")


# --- the real AEAD round-trip (skips cleanly without cryptography) -------


@pytest.mark.skipif(not _crypto.is_available(), reason="needs cryptography")
@pytest.mark.parametrize("data", [b"", b"secret bytes", os.urandom(256), b"FLGT-flight-body"])
def test_aead_round_trip(data):
    blob = _crypto.encrypt_bytes(data, "pw")
    assert blob[: len(_crypto.MAGIC)] == _crypto.MAGIC
    assert _crypto.decrypt_bytes(blob, "pw") == data


@pytest.mark.skipif(not _crypto.is_available(), reason="needs cryptography")
def test_aead_wrong_passphrase_fails():
    blob = _crypto.encrypt_bytes(b"secret", "right")
    with pytest.raises(_crypto.DecryptError):
        _crypto.decrypt_bytes(blob, "wrong")


@pytest.mark.skipif(not _crypto.is_available(), reason="needs cryptography")
@pytest.mark.parametrize("flip_at", [8, 20, -1])
def test_aead_tamper_detected(flip_at):
    blob = bytearray(_crypto.encrypt_bytes(b"secret payload", "pw"))
    blob[flip_at] ^= 0xFF  # corrupt a byte (header or ciphertext)
    with pytest.raises(_crypto.DecryptError):
        _crypto.decrypt_bytes(bytes(blob), "pw")


@pytest.mark.skipif(not _crypto.is_available(), reason="needs cryptography")
def test_encrypt_then_decrypt_file(tmp_path):
    src = tmp_path / "in.flight"
    src.write_bytes(b"FLGT-a-flight-body")
    enc = _crypto.encrypt_file(src, "pw")
    assert enc == src.with_suffix(".flight.enc")
    assert _crypto.looks_encrypted(enc)
    dec = _crypto.decrypt_file(enc, "pw")  # .enc stripped
    assert dec == src.with_suffix("")  # in
    assert dec.read_bytes() == b"FLGT-a-flight-body"
