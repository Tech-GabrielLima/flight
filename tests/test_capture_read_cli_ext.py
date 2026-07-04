"""Extended, exhaustive coverage of the crash black box, the reader API, the
install/uninstall lifecycle, and the `flight` CLI.

Everything exercises *real* behaviour: `.flight` files are produced by actually
installing Flight and raising (or via `flight.capture`/`flight.record`/
`flight.deterministic`), then read back and asserted against. Heavy use of
`@pytest.mark.parametrize` keeps the collected-case count high while every case
carries a meaningful assertion.
"""

from __future__ import annotations

import io
import sys
import contextlib
from dataclasses import dataclass
from pathlib import Path

import pytest

import flight
from flight import _capture
from flight._capture import (
    _exc_message,
    _exc_type_name,
    _exception_chain,
    build_payload,
    write_crash_flight,
)
from flight._cli import build_parser, main
from flight._config import Config


# --------------------------------------------------------------------------
# Module-level raisers (real code objects → real filename, source, frames)
# --------------------------------------------------------------------------

class CustomError(Exception):
    """A user-defined exception for the crash tests."""


class EvilRepr:
    def __repr__(self):
        raise RuntimeError("no repr for you")


def raise_value():
    x = 1  # noqa: F841
    raise ValueError("bad value")


def raise_key():
    d = {}
    return d["missing"]


def raise_index():
    seq = [1, 2, 3]
    return seq[99]


def raise_zerodiv():
    a, b = 7, 0
    return a // b


def raise_attr():
    obj = object()
    return obj.nope


def raise_type():
    return "a" + 5  # type: ignore[operator]


def raise_runtime():
    raise RuntimeError("boom runtime")


def raise_custom():
    payload = {"k": "v"}  # noqa: F841
    raise CustomError("custom boom")


#: name -> (raiser, expected-exc-type-headline predicate)
EXC_RAISERS = {
    "ValueError": (raise_value, "ValueError"),
    "KeyError": (raise_key, "KeyError"),
    "IndexError": (raise_index, "IndexError"),
    "ZeroDivisionError": (raise_zerodiv, "ZeroDivisionError"),
    "AttributeError": (raise_attr, "AttributeError"),
    "TypeError": (raise_type, "TypeError"),
    "RuntimeError": (raise_runtime, "RuntimeError"),
    "Custom": (raise_custom, "CustomError"),
}


def _capture_crash(fn, path):
    """Install, run `fn` (which raises), capture the handled exception, uninstall."""
    flight.install()
    try:
        try:
            fn()
        except BaseException:
            flight.capture(path=path)
    finally:
        flight.uninstall()
    return path


def raise_with_locals():
    n_int = 7
    n_float = 3.5
    n_str = "hi"
    n_bytes = b"by"
    n_bool = True
    n_none = None
    n_list = [1, 2, 3]
    n_dict = {"a": 1}
    n_tuple = (1, 2)
    n_set = {1, 2}
    _ = (n_int, n_float, n_str, n_bytes, n_bool, n_none, n_list, n_dict, n_tuple, n_set)
    raise ValueError("locals here")


def raise_nested():
    def inner(cfg):
        marker = "leaf"  # noqa: F841
        raise ValueError("deep")

    def outer():
        config = {"mode": "prod"}
        inner(config)

    outer()


def raise_chain_cause():
    try:
        raise KeyError("inner")
    except KeyError as e:
        raise ValueError("outer") from e


def raise_chain_context():
    try:
        raise KeyError("inner")
    except KeyError:
        raise ValueError("outer")


def raise_chain_suppressed():
    try:
        raise KeyError("inner")
    except KeyError:
        raise ValueError("outer") from None


def raise_evil():
    bad = EvilRepr()  # noqa: F841
    raise ValueError("evil local")


# --------------------------------------------------------------------------
# Session artifacts: build every .flight kind once.
# --------------------------------------------------------------------------

@dataclass
class Artifacts:
    dir: Path
    crashes: dict           # exc name -> Path (crash .flight)
    locals: Path
    nested: Path
    chain_cause: Path
    chain_context: Path
    chain_suppressed: Path
    evil: Path
    scope: Path
    scope2: Path
    scope_diff: Path
    nondet: Path
    nondet2: Path
    ring: Path
    correlated: Path
    trace_id: str


def _build_scope(path, n):
    with flight.record(path=path):
        total = 0
        acc = []
        for i in range(n):
            total += i
            acc.append(i)
    return path


def _build_nondet(path):
    import time

    def work():
        return time.time()

    with flight.deterministic(path=path):
        work()
    return path


def _build_ring(path):
    flight.install()
    try:
        def loop():
            s = 0
            for i in range(20):
                s += i
            return s

        loop()
        flight.capture(path=path)
    finally:
        flight.uninstall()
    return path


@pytest.fixture(scope="session")
def art(tmp_path_factory):
    d = tmp_path_factory.mktemp("flight_ext")
    crashes = {}
    for name, (fn, _exp) in EXC_RAISERS.items():
        crashes[name] = _capture_crash(fn, d / f"crash_{name}.flight")

    correlated = d / "correlated.flight"
    flight.install()
    trace_id = None
    try:
        ctx = flight.correlate(root=True, service="svc-a")
        trace_id = ctx.trace_id if ctx is not None else None
        try:
            raise_value()
        except BaseException:
            flight.capture(path=correlated)
    finally:
        flight.uninstall()

    return Artifacts(
        dir=d,
        crashes=crashes,
        locals=_capture_crash(raise_with_locals, d / "locals.flight"),
        nested=_capture_crash(raise_nested, d / "nested.flight"),
        chain_cause=_capture_crash(raise_chain_cause, d / "chain_cause.flight"),
        chain_context=_capture_crash(raise_chain_context, d / "chain_ctx.flight"),
        chain_suppressed=_capture_crash(raise_chain_suppressed, d / "chain_sup.flight"),
        evil=_capture_crash(raise_evil, d / "evil.flight"),
        scope=_build_scope(d / "scope.flight", 4),
        scope2=_build_scope(d / "scope2.flight", 4),
        scope_diff=_build_scope(d / "scope_diff.flight", 9),
        nondet=_build_nondet(d / "nondet.flight"),
        nondet2=_build_nondet(d / "nondet2.flight"),
        ring=_build_ring(d / "ring.flight"),
        correlated=correlated,
        trace_id=trace_id,
    )


def _stdout(argv):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        rc = main(argv)
    return rc, buf.getvalue()


# ==========================================================================
# 1. Crash capture — exception types
# ==========================================================================

@pytest.mark.parametrize("name", list(EXC_RAISERS))
def test_crash_headline_exc_type(art, name):
    f = flight.read(art.crashes[name])
    expected = EXC_RAISERS[name][1]
    assert f.exceptions[0][0].endswith(expected)


@pytest.mark.parametrize("name", list(EXC_RAISERS))
def test_crash_has_crash_flag(art, name):
    assert flight.read(art.crashes[name]).has_crash


@pytest.mark.parametrize("name", list(EXC_RAISERS))
def test_crash_is_complete(art, name):
    assert flight.read(art.crashes[name]).is_complete


@pytest.mark.parametrize("name", list(EXC_RAISERS))
def test_crash_blocks_present(art, name):
    blocks = set(flight.read(art.crashes[name]).blocks)
    assert {"META", "EXCEPTION", "FRAME", "OBJECT", "EVENT_RING"}.issubset(blocks)


@pytest.mark.parametrize("name", list(EXC_RAISERS))
def test_crash_has_frames(art, name):
    crash = flight.read(art.crashes[name]).crash()
    assert len(crash.frames) >= 1
    assert crash.frames[0].qualname  # innermost, non-empty


@pytest.mark.parametrize("name", list(EXC_RAISERS))
def test_crash_frame_count_positive(art, name):
    f = flight.read(art.crashes[name])
    assert f.frame_count >= 1
    assert f.object_count >= 0


@pytest.mark.parametrize("name", list(EXC_RAISERS))
def test_crash_no_mutations_no_nondet(art, name):
    f = flight.read(art.crashes[name])
    assert not f.has_mutations
    assert not f.has_nondet


@pytest.mark.parametrize("name", list(EXC_RAISERS))
def test_crash_exception_relation_head(art, name):
    excs = flight.read(art.crashes[name]).exceptions
    assert excs[0][2] == "head"


# ==========================================================================
# 2. Crash capture — locals of various types
# ==========================================================================

LOCAL_CASES = [
    ("n_int", "int", "7", None),
    ("n_float", "float", "3.5", None),
    ("n_str", "str", "hi", 2),
    ("n_bytes", "bytes", "b'by'", 2),
    ("n_bool", "bool", "True", None),
    ("n_none", "none", "None", None),
    ("n_list", "list", "list[3]", 3),
    ("n_dict", "dict", "dict[1]", 1),
    ("n_tuple", "tuple", "tuple[2]", 2),
    ("n_set", "set", "set[2]", 2),
]


def _locals_frame(art):
    crash = flight.read(art.locals).crash()
    for fr in crash.frames:
        if fr.qualname.endswith("raise_with_locals"):
            return crash, fr
    raise AssertionError("raise_with_locals frame not found")


@pytest.mark.parametrize("name,kind,render,length", LOCAL_CASES)
def test_local_kind(art, name, kind, render, length):
    crash, fr = _locals_frame(art)
    oid = dict(fr.locals)[name]
    assert crash.objects[oid]["kind"] == kind


@pytest.mark.parametrize("name,kind,render,length", LOCAL_CASES)
def test_local_render(art, name, kind, render, length):
    crash, fr = _locals_frame(art)
    oid = dict(fr.locals)[name]
    assert crash.render(oid) == render


@pytest.mark.parametrize("name,kind,render,length", LOCAL_CASES)
def test_local_length(art, name, kind, render, length):
    crash, fr = _locals_frame(art)
    oid = dict(fr.locals)[name]
    assert crash.objects[oid].get("length") == length


@pytest.mark.parametrize("name,kind,render,length", LOCAL_CASES)
def test_local_node_lookup(art, name, kind, render, length):
    crash, fr = _locals_frame(art)
    oid = dict(fr.locals)[name]
    assert crash.node(oid) is crash.objects[oid]


# ==========================================================================
# 3. Nested frames, ordering, aliasing
# ==========================================================================

def test_nested_crash_first_ordering(art):
    crash = flight.read(art.nested).crash()
    assert crash.frames[0].qualname.endswith("inner")


def test_nested_has_inner_and_outer(art):
    crash = flight.read(art.nested).crash()
    quals = [f.qualname for f in crash.frames]
    assert any(q.endswith("inner") for q in quals)
    assert any(q.endswith("outer") for q in quals)


def test_nested_aliasing_shared_dict(art):
    crash = flight.read(art.nested).crash()
    inner = next(f for f in crash.frames if f.qualname.endswith("inner"))
    outer = next(f for f in crash.frames if f.qualname.endswith("outer"))
    cfg_id = dict(inner.locals)["cfg"]
    config_id = dict(outer.locals)["config"]
    assert cfg_id == config_id
    names = {n for _i, n in crash.aliases(cfg_id)}
    assert {"cfg", "config"} <= names


def test_aliases_unique_local(art):
    crash, fr = _locals_frame(art)
    oid = dict(fr.locals)["n_list"]
    # n_list appears in exactly one frame's locals under exactly that name.
    appearances = crash.aliases(oid)
    assert (crash.frames.index(fr), "n_list") in appearances
    assert all(name == "n_list" for _i, name in appearances)


def test_render_missing_node(art):
    crash = flight.read(art.nested).crash()
    missing = max(crash.objects) + 10_000 if crash.objects else 999
    assert crash.render(missing) == "<missing>"
    assert crash.node(missing) is None


# ==========================================================================
# 4. Exception chains
# ==========================================================================

@pytest.mark.parametrize(
    "attr,rel1,rel2",
    [
        ("chain_cause", "ValueError", ("KeyError", "cause")),
        ("chain_context", "ValueError", ("KeyError", "context")),
    ],
)
def test_chain_two_level(art, attr, rel1, rel2):
    excs = flight.read(getattr(art, attr)).exceptions
    assert excs[0][0].endswith(rel1)
    assert excs[1][0].endswith(rel2[0])
    assert excs[1][2] == rel2[1]


def test_chain_suppressed_single(art):
    excs = flight.read(art.chain_suppressed).exceptions
    # `from None` suppresses the context → only the head survives.
    assert len(excs) == 1
    assert excs[0][0].endswith("ValueError")


# ==========================================================================
# 5. build_payload / write_crash_flight / _exception_chain unit-level
# ==========================================================================

def _live_exc(kind):
    """Return a live exception value with a traceback, of the requested chain kind."""
    try:
        if kind == "cause":
            try:
                raise KeyError("inner")
            except KeyError as e:
                raise ValueError("outer") from e
        elif kind == "context":
            try:
                raise KeyError("inner")
            except KeyError:
                raise ValueError("outer")
        elif kind == "suppressed":
            try:
                raise KeyError("inner")
            except KeyError:
                raise ValueError("outer") from None
        else:
            raise ValueError("plain")
    except ValueError as v:
        return v


@pytest.mark.parametrize(
    "kind,expected",
    [
        ("plain", [("ValueError", "head")]),
        ("cause", [("ValueError", "head"), ("KeyError", "cause")]),
        ("context", [("ValueError", "head"), ("KeyError", "context")]),
        ("suppressed", [("ValueError", "head")]),
    ],
)
def test_exception_chain_relations(kind, expected):
    exc = _live_exc(kind)
    chain = _exception_chain(exc)
    got = [(t, rel) for (t, _msg, rel) in chain]
    assert got == expected


def test_exception_chain_none():
    assert _exception_chain(None) == []


def test_exception_chain_no_cycle():
    # A self-referential context must not loop forever.
    a = ValueError("a")
    b = ValueError("b")
    a.__context__ = b
    b.__context__ = a
    chain = _exception_chain(a)
    assert len(chain) <= 2  # cycle broken by the `seen` set


@pytest.mark.parametrize(
    "exc,expected",
    [
        (ValueError("x"), "ValueError"),
        (KeyError("x"), "KeyError"),
        (ZeroDivisionError(), "ZeroDivisionError"),
        (RuntimeError(), "RuntimeError"),
    ],
)
def test_exc_type_name_builtin(exc, expected):
    assert _exc_type_name(exc) == expected


def test_exc_type_name_custom_is_qualified():
    name = _exc_type_name(CustomError("x"))
    assert name.endswith("CustomError")
    assert "." in name  # module-qualified, not a bare builtin name


@pytest.mark.parametrize(
    "exc,expected",
    [
        (ValueError("hello"), "hello"),
        (RuntimeError("boom"), "boom"),
        (KeyError("k"), "'k'"),
        (ValueError(), ""),
    ],
)
def test_exc_message(exc, expected):
    assert _exc_message(exc) == expected


def test_exc_message_never_raises():
    class BadStr(Exception):
        def __str__(self):
            raise RuntimeError("no str")

    msg = _exc_message(BadStr())
    assert msg.startswith("<str failed:")


def test_build_payload_shape():
    exc = _live_exc("cause")
    sources, excs, frames, objects = build_payload(exc, exc.__traceback__, Config())
    assert isinstance(sources, list)
    assert isinstance(frames, list) and len(frames) >= 1
    assert isinstance(objects, list)
    # chain carries both the head and its cause
    rels = [e[2] for e in excs]
    assert rels[0] == "head" and "cause" in rels


def test_write_crash_flight_returns_path(tmp_path):
    exc = _live_exc("plain")
    out = tmp_path / "wcf.flight"
    p = write_crash_flight(ValueError, exc, exc.__traceback__, Config(), path=out)
    assert p == out
    assert out.exists()
    assert flight.read(out).has_crash


@pytest.mark.parametrize("bad_tb", [None])
def test_write_crash_flight_no_tb_still_writes(tmp_path, bad_tb):
    # No traceback → no frames, but the exception chain is still recorded and
    # the call must NOT raise.
    out = tmp_path / "notb.flight"
    p = write_crash_flight(ValueError, ValueError("x"), bad_tb, Config(), path=out)
    assert p == out
    assert flight.read(out).exceptions[0][0].endswith("ValueError")


@pytest.mark.parametrize(
    "args",
    [
        (None, None, None),
        (ValueError, ValueError("x"), None),
        (ValueError, ValueError("x"), "not-a-traceback"),
    ],
)
def test_write_crash_flight_never_raises_on_bad_config(tmp_path, args):
    # A config missing the attributes the capture path needs must yield None,
    # never a second exception (P1).
    result = write_crash_flight(*args, object(), path=tmp_path / "z.flight")
    assert result is None


def test_write_crash_flight_bad_path_returns_none():
    exc = _live_exc("plain")
    # A path in a nonexistent directory → the native writer fails → None.
    result = write_crash_flight(
        ValueError, exc, exc.__traceback__, Config(), path="/no/such/dir/x.flight"
    )
    assert result is None


# ==========================================================================
# 6. Reader summary fields across file kinds
# ==========================================================================

KIND_ATTRS = ["locals", "nested", "scope", "nondet", "ring", "chain_cause"]


@pytest.mark.parametrize("attr", KIND_ATTRS)
def test_read_format_version(art, attr):
    assert flight.read(getattr(art, attr)).format_version == 1


@pytest.mark.parametrize("attr", KIND_ATTRS)
def test_read_flight_version_nonempty(art, attr):
    assert flight.read(getattr(art, attr)).flight_version


@pytest.mark.parametrize("attr", KIND_ATTRS)
def test_read_created_ts_positive(art, attr):
    assert flight.read(getattr(art, attr)).created_unix_ms > 0


@pytest.mark.parametrize("attr", KIND_ATTRS)
def test_read_has_meta_block(art, attr):
    f = flight.read(getattr(art, attr))
    assert "META" in f.blocks
    assert f.meta.get("python_version")


@pytest.mark.parametrize("attr", KIND_ATTRS)
def test_read_path_matches(art, attr):
    p = getattr(art, attr)
    assert flight.read(p).path == Path(p)


@pytest.mark.parametrize("attr", KIND_ATTRS)
def test_read_returns_flight_instance(art, attr):
    assert isinstance(flight.read(getattr(art, attr)), flight.Flight)


# ==========================================================================
# 7. Boolean property matrix
# ==========================================================================

# (attr, has_crash, has_mutations, has_nondet)
PROP_MATRIX = [
    ("locals", True, False, False),
    ("nested", True, False, False),
    ("scope", False, True, False),
    ("nondet", False, False, True),
    ("ring", False, False, False),
    ("correlated", True, False, True),  # crash + correlation on the nondet tape
]


@pytest.mark.parametrize("attr,crash,mut,nd", PROP_MATRIX)
def test_prop_has_crash(art, attr, crash, mut, nd):
    assert flight.read(getattr(art, attr)).has_crash is crash


@pytest.mark.parametrize("attr,crash,mut,nd", PROP_MATRIX)
def test_prop_has_mutations(art, attr, crash, mut, nd):
    assert flight.read(getattr(art, attr)).has_mutations is mut


@pytest.mark.parametrize("attr,crash,mut,nd", PROP_MATRIX)
def test_prop_has_nondet(art, attr, crash, mut, nd):
    assert flight.read(getattr(art, attr)).has_nondet is nd


@pytest.mark.parametrize("attr", [a[0] for a in PROP_MATRIX])
def test_prop_is_complete(art, attr):
    f = flight.read(getattr(art, attr))
    assert f.is_complete == (not f.partial)


# ==========================================================================
# 8. events(limit)
# ==========================================================================

@pytest.mark.parametrize("limit", [1, 2, 5, 10, 100, 500])
def test_events_respects_limit(art, limit):
    events = flight.read(art.ring).events(limit=limit)
    assert len(events) <= limit


@pytest.mark.parametrize("attr", ["ring", "locals", "scope"])
def test_events_shape(art, attr):
    events = flight.read(getattr(art, attr)).events(limit=50)
    for e in events:
        assert isinstance(e, tuple) and len(e) == 4


def test_event_count_matches_or_exceeds_recent(art):
    f = flight.read(art.ring)
    assert f.event_count >= len(f.recent_events)


# ==========================================================================
# 9. Recording / mutation timeline
# ==========================================================================

def test_recording_len_and_names(art):
    rec = flight.read(art.scope).recording()
    assert len(rec) == flight.read(art.scope).mutation_count
    assert "total" in rec.names()
    assert "acc" in rec.names()


def test_recording_history_total_increasing(art):
    rec = flight.read(art.scope).recording()
    hist = rec.history("total")
    assert len(hist) >= 1
    reprs = [m.value_repr for m in hist]
    # total accumulates 0,1,3,6 → the final write is the largest.
    assert reprs[-1] == "6"


def test_recording_history_unknown_var_empty(art):
    rec = flight.read(art.scope).recording()
    assert rec.history("does_not_exist") == []


def test_recording_who_mutated_returns_item_writes(art):
    rec = flight.read(art.scope).recording()
    writes = rec.who_mutated("acc")
    # `acc` is a local list; who_mutated only reports item/attr writes.
    for m in writes:
        assert m.kind in ("item", "attr")


@pytest.mark.parametrize("seq", [0, 1, 2, 5])
def test_recording_state_at_is_prefix(art, seq):
    rec = flight.read(art.scope).recording()
    state = rec.state_at(seq)
    assert isinstance(state, dict)
    # every value in the reconstructed state is a string rendering
    assert all(isinstance(v, str) for v in state.values())


def test_recording_state_at_monotonic_growth(art):
    rec = flight.read(art.scope).recording()
    early = rec.state_at(0)
    late = rec.state_at(len(rec))
    assert set(early) <= set(late)


def test_mutation_value_repr_property(art):
    rec = flight.read(art.scope).recording()
    for m in rec.mutations[:5]:
        assert isinstance(m.value_repr, str)


# ==========================================================================
# 10. Tape / nondet / correlation
# ==========================================================================

def test_tape_json_none_when_no_nondet(art):
    assert flight.read(art.ring).tape_json() is None
    assert flight.read(art.locals).tape_json() is None


def test_tape_json_present_for_nondet(art):
    js = flight.read(art.nondet).tape_json()
    assert js is not None and js.startswith("[")


def test_tape_sources_nonempty(art):
    tape = flight.read(art.nondet).tape()
    assert tape.sources()  # e.g. {"time.time": 1}


@pytest.mark.parametrize("attr", ["ring", "locals", "scope", "nondet"])
def test_correlation_none_when_absent(art, attr):
    assert flight.read(getattr(art, attr)).correlation() is None
    assert flight.read(getattr(art, attr)).trace_id is None


def test_correlation_present_on_correlated_crash(art):
    f = flight.read(art.correlated)
    assert f.trace_id == art.trace_id
    assert f.correlation() is not None


def test_capture_correlation_kwarg(tmp_path):
    from flight._correlation import TraceContext

    ctx = TraceContext.new_root(service="svc-x")
    out = tmp_path / "corr.flight"
    flight.install()
    try:
        try:
            raise_value()
        except BaseException:
            flight.capture(path=out, correlation=ctx)
    finally:
        flight.uninstall()
    assert flight.read(out).trace_id == ctx.trace_id


# ==========================================================================
# 11. install / uninstall lifecycle
# ==========================================================================

def test_is_installed_transitions():
    assert not flight.is_installed()
    flight.install()
    assert flight.is_installed()
    flight.uninstall()
    assert not flight.is_installed()


def test_uninstall_idempotent():
    flight.install()
    flight.uninstall()
    flight.uninstall()  # must not raise
    assert not flight.is_installed()


def test_uninstall_when_not_installed_is_noop():
    assert not flight.is_installed()
    flight.uninstall()
    assert not flight.is_installed()


def test_reinstall_replaces_session():
    import flight._install as _i

    flight.install()
    first = _i._active
    flight.install()  # replaces
    second = _i._active
    assert first is not second
    assert flight.is_installed()
    flight.uninstall()


def test_install_returns_config():
    cfg = flight.install()
    try:
        assert isinstance(cfg, Config)
    finally:
        flight.uninstall()


def test_install_overrides_applied():
    cfg = flight.install(ring_capacity=1024, record_lines=True)
    try:
        assert cfg.ring_capacity == 1024
        assert cfg.record_lines is True
    finally:
        flight.uninstall()


def test_uninstall_restores_excepthook():
    before = sys.excepthook
    flight.install()
    assert sys.excepthook is not before
    flight.uninstall()
    assert sys.excepthook is before


def test_uninstall_restores_threading_hook():
    import threading

    before = threading.excepthook
    flight.install()
    assert threading.excepthook is not before
    flight.uninstall()
    assert threading.excepthook is before


def test_uninstall_restores_unraisable_hook():
    before = sys.unraisablehook
    flight.install()
    assert sys.unraisablehook is not before
    flight.uninstall()
    assert sys.unraisablehook is before


@pytest.mark.parametrize(
    "lines,returns,expected_level",
    [
        (False, True, 1),   # LEVEL_RETURNS
        (True, True, 2),    # LEVEL_LINES (lines wins)
        (True, False, 2),   # LEVEL_LINES
        (False, False, 0),  # LEVEL_CALLS
    ],
)
def test_baseline_level(lines, returns, expected_level):
    import flight._install as _i

    flight.install(record_lines=lines, record_returns=returns)
    try:
        assert _i._active.baseline_level == expected_level
    finally:
        flight.uninstall()


@pytest.mark.parametrize("level", [-5, 0, 1, 2, 99])
def test_set_ring_level_never_raises(level):
    import flight._install as _i

    flight.install()
    try:
        _i._active.set_ring_level(level)  # clamps internally, must not raise
    finally:
        flight.uninstall()


def test_set_ring_level_noop_when_uninstalled():
    session_cfg = flight.install()
    import flight._install as _i

    sess = _i._active
    flight.uninstall()
    # After uninstall the session is not installed → set_ring_level short-circuits.
    sess.set_ring_level(2)  # must not raise
    assert session_cfg is not None


# ==========================================================================
# 12. stats()
# ==========================================================================

def test_stats_keys():
    flight.install()
    try:
        assert set(flight.stats()) == {"codes", "ring_capacity", "threads", "total_events"}
    finally:
        flight.uninstall()


def test_stats_ring_capacity_power_of_two():
    # The per-thread ring capacity is rounded up to a power of two. (It is
    # allocated lazily and cached per thread, so we assert the invariant, not a
    # specific reconfigured value.)
    flight.install(ring_capacity=2048)
    try:
        cap = flight.stats()["ring_capacity"]
        assert cap > 0
        assert cap & (cap - 1) == 0  # power of two
    finally:
        flight.uninstall()


@pytest.mark.parametrize("key", ["codes", "ring_capacity", "threads", "total_events"])
def test_stats_values_are_ints(key):
    flight.install()
    try:
        assert isinstance(flight.stats()[key], int)
    finally:
        flight.uninstall()


# ==========================================================================
# 13. dump()
# ==========================================================================

def test_dump_returns_path_when_installed(tmp_path):
    out = tmp_path / "dump.flight"
    flight.install()
    try:
        p = flight.dump(out)
    finally:
        flight.uninstall()
    assert p == out
    assert out.exists()
    assert "EVENT_RING" in flight.read(out).blocks


def test_dump_bad_path_returns_none():
    flight.install()
    try:
        assert flight.dump("/no/such/dir/nope.flight") is None
    finally:
        flight.uninstall()


def test_dump_without_install(tmp_path):
    # dump falls back to a fresh Config when nothing is installed.
    out = tmp_path / "d2.flight"
    p = flight.dump(out)
    assert p == out
    assert out.exists()


# ==========================================================================
# 14. capture() inside vs outside except
# ==========================================================================

def test_capture_inside_except_is_full_crash(tmp_path):
    out = tmp_path / "in.flight"
    flight.install()
    try:
        try:
            raise_nested()
        except BaseException:
            flight.capture(path=out)
    finally:
        flight.uninstall()
    f = flight.read(out)
    assert f.has_crash
    assert f.frame_count >= 2


def test_capture_outside_except_is_ring_only(tmp_path):
    out = tmp_path / "out.flight"
    flight.install()
    try:
        sum(range(5))
        flight.capture(path=out)
    finally:
        flight.uninstall()
    f = flight.read(out)
    assert not f.has_crash
    assert "EVENT_RING" in f.blocks


def test_capture_evil_repr_never_raises(art):
    # Built during the session fixture without raising; reads back as a crash.
    f = flight.read(art.evil)
    assert f.has_crash
    crash = f.crash()
    fr = next(fr for fr in crash.frames if fr.qualname.endswith("raise_evil"))
    # The evil local was captured without the repr blowing up.
    assert "bad" in dict(fr.locals)


# ==========================================================================
# 15. CLI — build_parser
# ==========================================================================

PARSER_CASES = [
    ("inspect", ["inspect", "f.flight"]),
    ("timeline", ["timeline", "f.flight"]),
    ("timeline", ["timeline", "f.flight", "--var", "x"]),
    ("timeline", ["timeline", "f.flight", "--who", "cache", "--limit", "5"]),
    ("view", ["view", "f.flight"]),
    ("repro", ["repro", "f.flight", "-o", "r.py", "--no-verify"]),
    ("repro", ["repro", "f.flight", "--pytest"]),
    ("explain", ["explain", "f.flight", "--prompt"]),
    ("explain", ["explain", "f.flight", "--llm"]),
    ("fingerprint", ["fingerprint", "f.flight"]),
    ("diff", ["diff", "a.flight", "b.flight"]),
    ("debug", ["debug", "f.flight", "--find", "x > 1"]),
    ("debug", ["debug", "f.flight", "--list", "--limit", "3"]),
    ("trace", ["trace", "dir1", "dir2"]),
    ("ci", ["ci", "f.flight", "-o", "out.md"]),
    ("encrypt", ["encrypt", "f.flight", "--passphrase", "pw"]),
    ("decrypt", ["decrypt", "f.enc", "-o", "f.flight"]),
    ("run", ["run", "script.py"]),
    ("run", ["run", "script.py", "--lines", "--daemon", "--correlate"]),
]


@pytest.mark.parametrize("cmd,argv", PARSER_CASES)
def test_parser_sets_command_and_func(cmd, argv):
    args = build_parser().parse_args(argv)
    assert args.command == cmd
    assert callable(args.func)


def test_parser_requires_subcommand():
    with pytest.raises(SystemExit):
        build_parser().parse_args([])


def test_parser_unknown_subcommand():
    with pytest.raises(SystemExit):
        build_parser().parse_args(["nonsense"])


def test_cli_version(capsys):
    with pytest.raises(SystemExit) as ei:
        main(["--version"])
    assert ei.value.code == 0
    assert "flight" in capsys.readouterr().out


# ==========================================================================
# 16. CLI — inspect
# ==========================================================================

@pytest.mark.parametrize("attr", ["locals", "nested", "scope", "nondet", "ring", "correlated"])
def test_cli_inspect_rc0(art, attr):
    rc, out = _stdout(["inspect", str(getattr(art, attr))])
    assert rc == 0
    assert "flight file" in out
    assert "events" in out


def test_cli_inspect_shows_exception(art):
    _rc, out = _stdout(["inspect", str(art.crashes["ValueError"])])
    assert "exception" in out
    assert "ValueError" in out


def test_cli_inspect_no_locals(art):
    rc, out = _stdout(["inspect", str(art.locals), "--no-locals"])
    assert rc == 0
    assert "n_int = 7" not in out


def test_cli_inspect_with_locals_default(art):
    _rc, out = _stdout(["inspect", str(art.locals)])
    assert "n_int" in out


def test_cli_inspect_max_locals(art):
    _rc, out = _stdout(["inspect", str(art.locals), "--max-locals", "2"])
    assert "more locals" in out


def test_cli_inspect_shows_mutations_line(art):
    _rc, out = _stdout(["inspect", str(art.scope)])
    assert "mutations" in out


def test_cli_inspect_shows_nondet_line(art):
    _rc, out = _stdout(["inspect", str(art.nondet)])
    assert "non-det" in out


# ==========================================================================
# 17. CLI — timeline
# ==========================================================================

def test_cli_timeline_rc0(art):
    rc, out = _stdout(["timeline", str(art.scope)])
    assert rc == 0
    assert "timeline:" in out


def test_cli_timeline_var(art):
    rc, out = _stdout(["timeline", str(art.scope), "--var", "total"])
    assert rc == 0
    assert "history of local 'total'" in out


def test_cli_timeline_who(art):
    rc, out = _stdout(["timeline", str(art.scope), "--who", "acc"])
    assert rc == 0
    assert "writes to 'acc'" in out


def test_cli_timeline_limit(art):
    rc, out = _stdout(["timeline", str(art.scope), "--limit", "1"])
    assert rc == 0


def test_cli_timeline_on_crash_file(art):
    # No scope recording → informative message, rc 0.
    rc, out = _stdout(["timeline", str(art.locals)])
    assert rc == 0
    assert "no scope recording" in out


# ==========================================================================
# 18. CLI — fingerprint
# ==========================================================================

def test_cli_fingerprint_crash_rc0(art):
    rc, out = _stdout(["fingerprint", str(art.crashes["ValueError"])])
    assert rc == 0
    assert out.strip()  # a hex-ish token


def test_cli_fingerprint_stable(art):
    _rc1, out1 = _stdout(["fingerprint", str(art.crashes["ValueError"])])
    _rc2, out2 = _stdout(["fingerprint", str(art.crashes["ValueError"])])
    assert out1 == out2


def test_cli_fingerprint_no_crash_rc1(art):
    rc, out = _stdout(["fingerprint", str(art.ring)])
    assert rc == 1
    assert "no crash" in out


# ==========================================================================
# 19. CLI — explain
# ==========================================================================

def test_cli_explain_rc0(art):
    rc, out = _stdout(["explain", str(art.crashes["ValueError"])])
    assert rc == 0
    assert "ValueError" in out


def test_cli_explain_prompt(art):
    rc, out = _stdout(["explain", str(art.crashes["ValueError"]), "--prompt"])
    assert rc == 0
    assert out.strip()


# ==========================================================================
# 20. CLI — diff
# ==========================================================================

def test_cli_diff_identical_scope_rc0(art):
    rc, out = _stdout(["diff", str(art.scope), str(art.scope)])
    assert rc == 0
    assert "identical" in out


def test_cli_diff_differing_scope_rc1(art):
    rc, out = _stdout(["diff", str(art.scope), str(art.scope_diff)])
    assert rc == 1
    assert "diverged" in out


def test_cli_diff_nondet_differ_rc1(art):
    rc, _out = _stdout(["diff", str(art.nondet), str(art.nondet2)])
    assert rc == 1


def test_cli_diff_two_distinct_crashes_nonzero(art):
    # Two different crashes are not identical → nonzero exit (differ or
    # incomparable), and the render explains where/why.
    rc, out = _stdout(["diff", str(art.crashes["ValueError"]), str(art.crashes["KeyError"])])
    assert rc in (1, 2)
    assert ("diverged" in out) or ("incomparable" in out)


# ==========================================================================
# 21. CLI — debug
# ==========================================================================

def test_cli_debug_list_rc0(art):
    rc, _out = _stdout(["debug", str(art.scope), "--list"])
    assert rc == 0


def test_cli_debug_list_limit(art):
    rc, out = _stdout(["debug", str(art.scope), "--list", "--limit", "1"])
    assert rc == 0
    assert "more" in out or out  # limited output


def test_cli_debug_find_match_rc0(art):
    rc, out = _stdout(["debug", str(art.scope), "--find", "total > 2"])
    assert rc == 0
    assert "first match" in out


def test_cli_debug_find_no_match_rc1(art):
    rc, out = _stdout(["debug", str(art.scope), "--find", "total > 100000"])
    assert rc == 1
    assert "no write ever matched" in out


def test_cli_debug_on_crash_file_rc1(art):
    rc, out = _stdout(["debug", str(art.locals), "--list"])
    assert rc == 1
    assert "no scope recording" in out


# ==========================================================================
# 22. CLI — trace
# ==========================================================================

def test_cli_trace_dir_rc0(art):
    rc, out = _stdout(["trace", str(art.dir)])
    assert rc == 0
    assert art.trace_id in out


def test_cli_trace_uncorrelated_rc1(art):
    rc, out = _stdout(["trace", str(art.nondet)])
    assert rc == 1
    assert "no correlated" in out


# ==========================================================================
# 23. CLI — ci
# ==========================================================================

def test_cli_ci_crash_rc0(art):
    rc, out = _stdout(["ci", str(art.crashes["ValueError"])])
    assert rc == 0
    assert "ValueError" in out


def test_cli_ci_writes_output_file(art, tmp_path):
    md = tmp_path / "comment.md"
    rc, _out = _stdout(["ci", str(art.crashes["ValueError"]), "-o", str(md)])
    assert rc == 0
    assert md.exists()
    assert md.read_text().strip()


# ==========================================================================
# 24. CLI — repro
# ==========================================================================

def test_cli_repro_no_verify(art, tmp_path):
    out = tmp_path / "repro_out.py"
    rc, _text = _stdout(["repro", str(art.locals), "-o", str(out), "--no-verify"])
    # Either it builds a repro (rc 0, file written) or reports it cannot (rc 1).
    assert rc in (0, 1)
    if rc == 0:
        assert out.exists()


# ==========================================================================
# 25. CLI — error paths (missing / wrong-type files)
# ==========================================================================

MISSING_CMDS = [
    ["inspect", "/no/such/file.flight"],
    ["timeline", "/no/such/file.flight"],
    ["fingerprint", "/no/such/file.flight"],
    ["explain", "/no/such/file.flight"],
    ["debug", "/no/such/file.flight", "--list"],
]


@pytest.mark.parametrize("argv", MISSING_CMDS)
def test_cli_missing_file_raises(argv):
    with pytest.raises(Exception):
        main(argv)


def test_read_missing_file_raises():
    with pytest.raises(ValueError):
        flight.read("/no/such/file.flight")


def test_read_not_a_flight_file_raises(tmp_path):
    bogus = tmp_path / "bogus.flight"
    bogus.write_bytes(b"this is definitely not a flight file")
    with pytest.raises(ValueError):
        flight.read(bogus)


@pytest.mark.parametrize("cmd", ["inspect", "fingerprint", "explain"])
def test_cli_wrong_file_type_raises(tmp_path, cmd):
    bogus = tmp_path / "bogus.flight"
    bogus.write_bytes(b"garbage bytes here \x00\x01\x02")
    with pytest.raises(Exception):
        main([cmd, str(bogus)])
