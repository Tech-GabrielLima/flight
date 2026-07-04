"""Coverage-closing tests across many modules (Phase 0-10).

Each test targets a specific previously-uncovered line/branch. Where a branch is
a genuinely defensive/unreachable P1 guard it is marked `# pragma: no cover` in
the source with a justification instead of being tested here (only one such line
exists: `_capture` line 86-87, the f_locals proxy guard).

No test here calls a real LLM or touches the network: the `_default_provider`
Anthropic path is exercised against a fake `anthropic` module injected into
`sys.modules`.
"""

from __future__ import annotations

import io
import sys
import types

import pytest

import flight


# ===========================================================================
# helpers
# ===========================================================================


def _capture_crash(path, fn, exc):
    """Run `fn` under Flight, capture the raised `exc` to a `.flight`."""
    flight.install()
    try:
        fn()
    except exc:
        flight.capture(path=str(path))
    finally:
        flight.uninstall()
    return str(path)


def _alias_crash(tmp_path):
    """A crash where the same dict is a local in two frames (aliased) and has
    children (a non-empty dict) — feeds viewer alias/expand/source branches."""

    def inner(shared):
        return shared["nope"]  # KeyError; `shared` is a dict with children

    def outer():
        data = {"a": 1, "b": 2}
        inner(data)

    return _capture_crash(tmp_path / "alias.flight", outer, KeyError)


def _chain_crash(tmp_path):
    """A crash carrying a two-link exception chain (RuntimeError from ValueError)."""

    def chained():
        try:
            int("not-an-int")
        except ValueError as e:
            raise RuntimeError("wrapped") from e

    return _capture_crash(tmp_path / "chain.flight", chained, RuntimeError)


def _nosource_crash(tmp_path):
    """A crash whose crash frame lives in exec'd code (`<string>`) so no source
    is captured for it."""
    ns: dict = {}
    exec("def f(x):\n    return x[5]\n", ns)

    def run():
        ns["f"]([1, 2, 3])

    return _capture_crash(tmp_path / "nosrc.flight", run, IndexError)


def _dunder_crash(tmp_path):
    """A crash frame that has a dunder local (skipped by context/repro builders).

    The crashing function lives in its own written-out source file, so the crash
    `.flight` embeds *that* controlled source (not this test module), letting a
    repro-script assertion look for tokens without matching this file's text."""
    src = (
        "def crash_with_dunder():\n"
        "    __weird__ = 123\n"
        "    x = []\n"
        "    return x[10]\n"
    )
    modfile = tmp_path / "dundermod.py"
    modfile.write_text(src)
    ns: dict = {}
    exec(compile(src, str(modfile), "exec"), ns)

    def run():
        ns["crash_with_dunder"]()

    return _capture_crash(tmp_path / "dunder.flight", run, IndexError)


# ===========================================================================
# _explain.py
# ===========================================================================


def test_explain_suspicion_none_node():
    from flight._explain import _suspicion

    assert _suspicion(None) is None  # line 57


def test_explain_build_context_crash_without_frames(monkeypatch):
    from flight import _read
    from flight._explain import build_context

    class FakeCrash:
        exceptions = [("ValueError", "boom", "head")]
        frames: list = []

    class FakeFlight:
        has_crash = True

        def crash(self):
            return FakeCrash()

    monkeypatch.setattr(_read, "read", lambda p: FakeFlight())
    ctx = build_context("x")  # line 81: `if not crash.frames: return ctx`
    assert ctx["exceptions"] and ctx["frames"] == []


def test_explain_build_context_skips_dunder_local(tmp_path):
    from flight._explain import build_context

    ctx = build_context(_dunder_crash(tmp_path))  # line 90 (dunder skipped)
    names = [loc["name"] for loc in ctx.get("locals", [])]
    assert "__weird__" not in names
    assert "x" in names


def test_explain_analyze_renders_exception_chain(tmp_path):
    from flight._explain import analyze, build_context

    summary, _suspects = analyze(build_context(_chain_crash(tmp_path)))  # lines 136-137
    assert "exception chain" in summary
    assert "RuntimeError" in summary and "ValueError" in summary


def test_explain_default_provider_without_key(monkeypatch):
    from flight._explain import _default_provider

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert _default_provider() is None  # lines 182-183


def test_explain_default_provider_without_anthropic(monkeypatch):
    from flight._explain import _default_provider

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setitem(sys.modules, "anthropic", None)  # import raises ImportError
    assert _default_provider() is None  # lines 186-187


def test_explain_default_provider_with_fake_anthropic(monkeypatch):
    from flight._explain import _default_provider

    calls = {}

    class _Messages:
        def create(self, **kw):
            calls.update(kw)
            return types.SimpleNamespace(content=[types.SimpleNamespace(text="the answer")])

    class _Anthropic:
        def __init__(self):
            self.messages = _Messages()

    fake = types.ModuleType("anthropic")
    fake.Anthropic = _Anthropic
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("FLIGHT_EXPLAIN_MODEL", "claude-test")
    monkeypatch.setitem(sys.modules, "anthropic", fake)

    provider = _default_provider()  # lines 184,185,188,197
    assert provider is not None
    assert provider("hello prompt") == "the answer"  # lines 190-195
    assert calls["model"] == "claude-test"


# ===========================================================================
# _repro.py
# ===========================================================================


def test_repro_reconstructor_missing_object():
    from flight._repro import _Reconstructor

    rec = _Reconstructor({})
    assert rec.build(999) == "None  # <missing object>"  # lines 57-58
    assert rec.approximate


def test_repro_reconstructor_container_build_failure():
    from flight._repro import _Reconstructor

    # A "dict" node whose items are not iterable pairs -> _build_container raises.
    rec = _Reconstructor({1: {"kind": "dict", "items": 12345}})
    var = rec.build(1)  # lines 68-70 (except -> approximate + failure line)
    assert rec.approximate
    assert any("failed to reconstruct" in ln for ln in rec.lines)
    assert var.startswith("_v")


def test_repro_reconstructor_unreconstructable_kind():
    from flight._repro import _Reconstructor

    rec = _Reconstructor({1: {"kind": "ndarray", "items": []}})
    rec.build(1)  # lines 102-103 (else branch)
    assert rec.approximate
    assert any("not reconstructable" in ln for ln in rec.lines)


def test_repro_scalar_edges():
    from flight._repro import _Reconstructor

    rec = _Reconstructor({})
    assert rec._scalar({"kind": "float", "repr": "inf"}) == "float('inf')"  # line 116
    assert rec._scalar({"kind": "str", "repr": "x", "truncated": True}) == "'x'"  # line 120
    assert rec._scalar({"kind": "bytes", "repr": "not-bytes", "truncated": True}) == "b''"  # 124
    assert rec.approximate
    assert rec._scalar({"kind": "mystery"}) == "None"  # line 126 (fallthrough)


def test_repro_key_literal_edges():
    from flight._repro import _key_literal

    assert _key_literal(None) == "None"  # line 140
    assert _key_literal("5") == "5"  # line 142
    assert _key_literal("True") == "True"  # line 144


def test_repro_build_repro_no_frames(monkeypatch):
    from flight import _repro
    from flight._repro import build_repro

    class FakeCrash:
        frames: list = []

    class FakeFlight:
        has_crash = True

        def crash(self):
            return FakeCrash()

    # _repro binds `read` at import time (top-level), so patch it on _repro.
    monkeypatch.setattr(_repro, "read", lambda p: FakeFlight())
    res = build_repro("x")  # line 296
    assert res.verified is False and "no frames" in res.reason


def test_repro_build_repro_no_source(tmp_path):
    from flight._repro import build_repro

    res = build_repro(_nosource_crash(tmp_path))  # line 300 (source not captured)
    assert res.verified is False
    assert "source not captured" in res.reason


def test_repro_build_repro_skips_dunder_local(tmp_path):
    from flight._repro import build_repro

    res = build_repro(_dunder_crash(tmp_path))  # line 309 (dunder skipped)
    assert res.script
    # The dunder local is skipped: it never appears in the reconstructed `_locals`
    # mapping (which emits `'<name>': <name>_ref,` for each kept local).
    locals_block = res.script.split("_locals = {", 1)[1].split("}", 1)[0]
    assert "__weird__" not in locals_block
    assert "'x': x_ref" in locals_block


def test_repro_verify_subprocess_failure(monkeypatch, tmp_path):
    from flight import _repro

    def boom(*a, **k):
        raise OSError("cannot spawn")

    monkeypatch.setattr(_repro.subprocess, "run", boom)
    fake = tmp_path / "repro.py"
    fake.write_text("print('x')\n")
    assert _repro._verify(fake) is False  # lines 365-366


def test_repro_write_repro_no_crash_returns_early(tmp_path):
    from flight._repro import write_repro

    # A ring-only .flight (no crash) -> build_repro yields an empty script ->
    # write_repro returns it without writing or verifying (line 347).
    p = tmp_path / "ring.flight"
    flight.install()
    try:
        sum(range(10))
        flight.dump(str(p))
    finally:
        flight.uninstall()
    res = write_repro(str(p))
    assert not res.script and res.path is None


# ===========================================================================
# _capture.py
# ===========================================================================


def test_capture_correlation_append_failure(tmp_path):
    from flight._capture import write_crash_flight
    from flight._config import Config

    class BadCtx:
        def to_nondet(self):
            raise RuntimeError("no nondet")

    try:
        raise ValueError("boom")
    except ValueError:
        et, ev, tb = sys.exc_info()
        path = write_crash_flight(
            et, ev, tb, Config(), path=str(tmp_path / "c.flight"), correlation=BadCtx()
        )  # lines 125-126 (except pass)
    assert path is not None
    assert flight.read(path).has_crash


def test_capture_read_source_failure(monkeypatch):
    from flight import _capture

    monkeypatch.setattr(_capture.linecache, "getlines", lambda *a, **k: (_ for _ in ()).throw(OSError()))
    assert _capture._read_source("some_real_file.py") is None  # lines 148-149


def test_capture_cwd_failure(monkeypatch):
    from flight import _capture

    monkeypatch.setattr(_capture.os, "getcwd", lambda: (_ for _ in ()).throw(OSError()))
    assert _capture._cwd() == ""  # lines 186-187


# ===========================================================================
# _ci.py
# ===========================================================================


def test_ci_render_truncates_long_stack(tmp_path):
    from flight._ci import render_comment

    def deep(n):
        if n == 0:
            return [][0]  # IndexError deep in the recursion (>12 frames)
        return deep(n - 1)

    path = _capture_crash(tmp_path / "deep.flight", lambda: deep(20), IndexError)
    md = render_comment(path)  # line 68 (`… N more frames`)
    assert "more frames" in md


def test_ci_fingerprint_failure_is_swallowed(monkeypatch, tmp_path):
    from flight import _fingerprint
    from flight._ci import render_comment

    def crash():
        return {}["missing"]

    path = _capture_crash(tmp_path / "fp.flight", crash, KeyError)
    monkeypatch.setattr(_fingerprint, "fingerprint", lambda p: (_ for _ in ()).throw(RuntimeError()))
    md = render_comment(path)  # lines 76-77 (except pass around fingerprint)
    assert "Fingerprint" not in md
    assert "KeyError" in md


def test_ci_md_escape_pipes_and_newlines():
    from flight._ci import _md_escape

    assert _md_escape("a|b\nc") == "a\\|b c"


# ===========================================================================
# _ddmin.py
# ===========================================================================


def test_ddmin_run_replay_divergence(monkeypatch):
    from flight import _nondet
    from flight._ddmin import _run

    def diverge(*a, **k):
        raise _nondet.ReplayDivergence("branch changed")

    monkeypatch.setattr(_nondet, "replay_tape", diverge)
    out = _run([(0, "random.random", "f", "0.5")], lambda: None, (), {})  # line 90
    assert out["diverged"] is True and out["raised"] is False


def test_ddmin_minimize_reads_file(tmp_path):
    import random

    from flight._ddmin import minimize

    def work():
        random.seed(1)
        return [random.randint(1, 6) for _ in range(4)]

    p = tmp_path / "m.flight"
    with flight.deterministic(str(p)):
        work()
    res = minimize(str(p), work)  # lines 135-137
    assert res is not None
    assert isinstance(res.total, int)


# ===========================================================================
# _fingerprint.py
# ===========================================================================


def test_fingerprint_skips_dunder_local(tmp_path):
    from flight._fingerprint import signature

    sig = signature(_dunder_crash(tmp_path))  # line 52 (dunder skipped)
    assert sig.exceptions  # a real crash signature was produced
    # only the non-dunder local kinds appear
    assert isinstance(sig.state_kinds, list)


# ===========================================================================
# _dap.py
# ===========================================================================


def test_dap_handler_exception_is_reported():
    from flight._dap import DebugAdapter

    a = DebugAdapter()
    # launch with a nonexistent program -> _load's read() raises -> handle's guard
    msgs = a.handle({"command": "launch", "seq": 1, "arguments": {"program": "/no/such.flight"}})
    assert msgs[0]["success"] is False  # lines 83-84


def test_dap_stacktrace_synthetic_frame_before_first_write(tmp_path):
    from flight._dap import DebugAdapter

    scope = tmp_path / "s.flight"
    with flight.record(path=str(scope)):
        acc = 0
        for i in range(3):
            acc += i
    a = DebugAdapter()
    a._load(str(scope))
    a._tt._pos = 0  # cursor before the first write -> current() is None
    msgs = a.handle({"command": "stackTrace", "seq": 2})  # line 134 synthetic frame
    frame = msgs[0]["body"]["stackFrames"][0]
    assert frame["name"] == "<recording start>"


def test_dap_serve_stops_on_eof():
    from flight._dap import serve

    serve(io.BytesIO(b""), io.BytesIO())  # line 356 (`break` when req is None)


# ===========================================================================
# _diff.py
# ===========================================================================


def test_diff_files_incomparable(monkeypatch):
    from flight import _read
    from flight._diff import diff_files

    class FakeF:
        has_mutations = False
        has_nondet = False

        def events(self):
            return []

    monkeypatch.setattr(_read, "read", lambda p: FakeF())
    d = diff_files("a", "b")  # line 187
    assert d.kind == "incomparable" and not d.identical


# ===========================================================================
# _read.py
# ===========================================================================


def test_read_correlation_guard(monkeypatch, tmp_path):
    from flight import _read

    def crash():
        return [][0]

    path = _capture_crash(tmp_path / "corr.flight", crash, IndexError)
    f = flight.read(path)
    monkeypatch.setattr(_read._core, "read_nondet", lambda *a, **k: (_ for _ in ()).throw(OSError()))
    assert f.correlation() is None  # lines 195-196


# ===========================================================================
# _serialize.py
# ===========================================================================


def test_serialize_int_repr_huge_guard():
    from flight import _serialize

    class Weird:
        def __repr__(self):
            raise ValueError("too big to repr")

        def bit_length(self):
            raise RuntimeError("no bit length")

    assert _serialize._int_repr(Weird()) == "<huge int>"  # lines 259-260


def test_serialize_safe_len_guard():
    from flight import _serialize

    assert _serialize._safe_len(object()) == 0  # lines 310-311 (len() raises)


# ===========================================================================
# _viewer_model.py
# ===========================================================================


class _FakeFrame:
    def __init__(self, file, lineno, locals_):
        self.file = file
        self.lineno = lineno
        self.locals = locals_


class _FakeCrash:
    def __init__(self, frames, objects, sources):
        self.frames = frames
        self.objects = objects
        self.sources = sources

    def render(self, oid):
        return f"<obj {oid}>"


def test_viewer_model_alias_index_excludes_shared_scalar():
    from flight._viewer_model import alias_index

    # oid 1 (an int) appears in two frames -> shared, but a scalar -> excluded.
    crash = _FakeCrash(
        frames=[_FakeFrame("f.py", 1, [("x", 1)]), _FakeFrame("f.py", 2, [("y", 1)])],
        objects={1: {"kind": "int", "items": []}},
        sources={},
    )
    assert alias_index(crash) == {}  # line 58 (scalar continue)


def test_viewer_model_object_detail_type_and_truncated():
    from flight._viewer_model import object_detail

    crash = _FakeCrash(frames=[], objects={}, sources={})
    crash.objects = {1: {"kind": "object", "type_name": "Foo", "repr": "<Foo>"}}
    lines = object_detail(crash, 1)  # line 91 (type_name present)
    assert any("type" in ln and "Foo" in ln for ln in lines)

    crash.objects = {2: {"kind": "str", "repr": "abc", "length": 3, "truncated": True}}
    lines = object_detail(crash, 2)  # line 97 (truncated)
    assert any("truncated" in ln for ln in lines)


def test_viewer_model_source_window_missing_source():
    from flight._viewer_model import source_window

    crash = _FakeCrash(frames=[_FakeFrame("gone.py", 7, [])], objects={}, sources={})
    rows, cur = source_window(crash, 0)  # line 114 (no text)
    assert rows == [] and cur == 7


# ===========================================================================
# _web.py
# ===========================================================================


def test_web_capture_request_swallows_errors():
    from flight._web import _capture_request

    # output_dir=None makes `output_dir / name` raise -> guarded -> None.
    assert _capture_request(None, "GET", "/x", None, None) is None  # lines 76-77


def test_web_wsgi_mkdir_failure_guarded(monkeypatch):
    import pathlib

    from flight._web import FlightWSGI

    monkeypatch.setattr(pathlib.Path, "mkdir", lambda *a, **k: (_ for _ in ()).throw(OSError()))
    mw = FlightWSGI(lambda e, s: [b""], install=False)  # lines 97-98
    assert mw is not None


def test_web_asgi_mkdir_failure_guarded(monkeypatch):
    import pathlib

    from flight._web import FlightASGI

    monkeypatch.setattr(pathlib.Path, "mkdir", lambda *a, **k: (_ for _ in ()).throw(OSError()))
    mw = FlightASGI(lambda s, r, snd: None, install=False)  # lines 163-164
    assert mw is not None


def test_web_asgi_header_decode_failure():
    from flight._web import _asgi_header

    class BadVal:
        def decode(self, _enc):
            raise UnicodeDecodeError("x", b"", 0, 1, "boom")

    scope = {"headers": [(b"traceparent", BadVal())]}
    assert _asgi_header(scope, b"traceparent") is None  # lines 187-188


def test_web_asgi_non_http_passthrough():
    import asyncio

    from flight._web import FlightASGI

    seen = {}

    async def app(scope, receive, send):
        seen["called"] = scope["type"]

    mw = FlightASGI(app, install=False)

    async def drive():
        # a non-http scope is passed straight through (lifespan/websocket)
        await mw({"type": "lifespan"}, None, None)  # lines 168-171

    asyncio.run(drive())
    assert seen["called"] == "lifespan"


# ===========================================================================
# _pytest.py
# ===========================================================================


class _FakeConfig:
    def __init__(self, opts):
        self._opts = opts

    def getoption(self, name):
        return self._opts.get(name)


class _FakeStash(dict):
    def get(self, key, default=None):  # noqa: A003
        return dict.get(self, key, default)


class _FakeItem:
    def __init__(self, config, nodeid="tests/test_x.py::test_a"):
        self.config = config
        self.nodeid = nodeid
        self.stash = _FakeStash()


def test_pytest_configure_mkdir_failure_guarded(monkeypatch):
    import pathlib

    from flight import _pytest

    monkeypatch.setattr(pathlib.Path, "mkdir", lambda *a, **k: (_ for _ in ()).throw(OSError()))
    config = _FakeConfig({"--flight": True, "--flight-dir": ".flight"})
    _pytest.pytest_configure(config)  # lines 86-87 (except pass)
    assert config._flight_enabled is True
    assert hasattr(config, "_flight_dir")


def test_pytest_runtest_call_install_failure(monkeypatch):
    from flight import _pytest

    monkeypatch.setattr(flight, "install", lambda **k: (_ for _ in ()).throw(RuntimeError("no install")))
    config = _FakeConfig({"--flight": True, "--flight-dir": ".flight", "--flight-lines": False})
    config._flight_enabled = True  # the hook gates on this attr (set by configure)
    config._flight_dir = ".flight"
    item = _FakeItem(config)
    gen = _pytest.pytest_runtest_call(item)
    next(gen)  # install raises -> except -> `yield` inside the guard (115-118)
    with pytest.raises(StopIteration):
        next(gen)  # the generator returns after yielding


def test_pytest_runtest_call_write_failure_guarded(monkeypatch, tmp_path):
    from flight import _pytest

    monkeypatch.setattr(_pytest, "_write_for", lambda item, excinfo: (_ for _ in ()).throw(RuntimeError()))
    config = _FakeConfig(
        {"--flight": True, "--flight-dir": str(tmp_path), "--flight-lines": False, "--flight-all": False}
    )
    config._flight_enabled = True
    config._flight_dir = str(tmp_path)
    config._flight_written = []
    item = _FakeItem(config)

    gen = _pytest.pytest_runtest_call(item)
    next(gen)  # install ok, advance to `outcome = yield`
    outcome = types.SimpleNamespace(excinfo=("ValueError", ValueError("x"), None))
    with pytest.raises(StopIteration):
        gen.send(outcome)  # finally: _write_for raises -> lines 132-133 (except pass)


def test_pytest_runtest_call_uninstall_failure_guarded(monkeypatch, tmp_path):
    from flight import _pytest

    real_uninstall = flight.uninstall

    def boom():
        real_uninstall()  # keep global state clean for the autouse fixture
        raise RuntimeError("uninstall boom")

    monkeypatch.setattr(flight, "uninstall", boom)
    config = _FakeConfig(
        {"--flight": True, "--flight-dir": str(tmp_path), "--flight-lines": False, "--flight-all": False}
    )
    config._flight_enabled = True
    config._flight_dir = str(tmp_path)
    config._flight_written = []
    item = _FakeItem(config)

    gen = _pytest.pytest_runtest_call(item)
    next(gen)  # install ok
    outcome = types.SimpleNamespace(excinfo=None)  # a pass, no --flight-all -> no write
    with pytest.raises(StopIteration):
        gen.send(outcome)  # finally: uninstall raises -> lines 137-138 (except pass)


def test_pytest_runtest_call_success_writes_and_stashes(tmp_path):
    from flight._pytest import _PATH_KEY, pytest_runtest_call

    config = _FakeConfig(
        {"--flight": True, "--flight-dir": str(tmp_path), "--flight-lines": False, "--flight-all": False}
    )
    config._flight_enabled = True
    config._flight_dir = str(tmp_path)
    config._flight_written = []
    item = _FakeItem(config, nodeid="tests/t.py::test_ok")

    gen = pytest_runtest_call(item)
    next(gen)  # install ok, at `outcome = yield`
    try:
        raise ValueError("recorded failure")
    except ValueError:
        excinfo = sys.exc_info()
    outcome = types.SimpleNamespace(excinfo=excinfo)
    with pytest.raises(StopIteration):
        gen.send(outcome)  # finally: _write_for writes a crash -> lines 128-131
    assert config._flight_written  # (nodeid, path) recorded
    assert item.stash.get(_PATH_KEY)  # path stashed for the report hook


def test_pytest_write_for_both_branches(tmp_path):
    from flight._pytest import _write_for

    config = _FakeConfig({})
    config._flight_dir = str(tmp_path)
    # distinct nodeids -> distinct dest files (so the ring write can't clobber the crash)
    crash_item = _FakeItem(config, nodeid="tests/t.py::test_crash")
    ring_item = _FakeItem(config, nodeid="tests/t.py::test_pass")
    flight.install()
    try:
        try:
            raise ValueError("boom")
        except ValueError:
            crash_path = _write_for(crash_item, sys.exc_info())  # lines 150-152 (crash detail)
        ring_path = _write_for(ring_item, None)  # line 153 (ring-only dump on a pass)
    finally:
        flight.uninstall()
    assert crash_path is not None and flight.read(crash_path).has_crash
    assert ring_path is not None


def test_pytest_makereport_surfaces_path():
    from flight._pytest import _PATH_KEY, pytest_runtest_makereport

    config = _FakeConfig({})
    item = _FakeItem(config)
    item.stash[_PATH_KEY] = "/tmp/x.flight"
    report = types.SimpleNamespace(when="call", sections=[], user_properties=[])
    outcome = types.SimpleNamespace(get_result=lambda: report)

    gen = pytest_runtest_makereport(item, None)
    next(gen)  # advance to `outcome = yield`
    with pytest.raises(StopIteration):
        gen.send(outcome)  # lines 167-168 (surface the path)
    assert report.sections and report.user_properties


def test_pytest_terminal_summary():
    from flight._pytest import pytest_terminal_summary

    class _TR:
        def __init__(self):
            self.seps: list = []
            self.lines: list = []

        def write_sep(self, ch, msg):
            self.seps.append(msg)

        def write_line(self, msg):
            self.lines.append(msg)

    # nothing written -> early return (lines 172-174)
    empty = _FakeConfig({})
    empty._flight_written = []
    pytest_terminal_summary(_TR(), 0, empty)

    # something written -> the summary block (lines 175-179)
    config = _FakeConfig({})
    config._flight_written = [("tests/t.py::test_a", "/tmp/a.flight")]
    tr = _TR()
    pytest_terminal_summary(tr, 0, config)
    assert tr.seps and tr.lines


# ===========================================================================
# _viewer.py  (Textual TUI, driven headlessly via Pilot)
# ===========================================================================

textual = pytest.importorskip("textual")

from flight._viewer import FlightViewer  # noqa: E402
from textual.widgets import Tree  # noqa: E402


def test_viewer_source_missing_and_exception_early_return(tmp_path):
    import asyncio

    app = FlightViewer(_nosource_crash(tmp_path))

    async def drive():
        async with app.run_test():
            # on_mount already called _show_source(0), whose frame's file has no
            # captured source -> the `if not rows` branch (line 135) ran.
            assert app.crash.frames[0].file not in app.crash.sources
            # _show_exception early-return when there are no exceptions (line 119).
            app.crash = types.SimpleNamespace(exceptions=[])
            app._show_exception()  # returns immediately without raising

    asyncio.run(drive())


def test_viewer_exception_chain_rendered(tmp_path):
    import asyncio

    app = FlightViewer(_chain_crash(tmp_path))

    async def drive():
        async with app.run_test():
            # on_mount -> _show_exception iterated the >1-link chain (line 123).
            assert len(app.crash.exceptions) >= 2

    asyncio.run(drive())


def test_viewer_expand_frame_highlight_and_aliases(tmp_path):
    import asyncio

    app = FlightViewer(_alias_crash(tmp_path))

    async def drive():
        async with app.run_test() as pilot:
            tree = app.query_one("#tree", Tree)
            frame_nodes = [n for n in tree.root.children if n.data and n.data[0] == "frame"]
            assert frame_nodes

            # frame highlight -> _show_source (line 168)
            app.on_tree_node_highlighted(types.SimpleNamespace(node=frame_nodes[0]))

            # expand an object node with children -> populate (lines 156-157)
            obj_nodes = [c for c in frame_nodes[0].children if c.data and c.data[0] == "obj"]
            expandable = [n for n in obj_nodes if n.allow_expand]
            assert expandable, "the aliased dict should be expandable"
            node = expandable[0]
            node.expand()
            await pilot.pause()
            assert node.children

            # action_aliases on a frame node -> "select an object" (lines 176-177)
            tree.move_cursor(frame_nodes[0])
            app.action_aliases()

            # action_aliases on an aliased object node -> "same object also in" (180-181)
            aliased = [n for n in obj_nodes if n.data[1] in app.aliases]
            assert aliased, "expected an aliased object across frames"
            tree.move_cursor(aliased[0])
            app.action_aliases()

    asyncio.run(drive())


def test_viewer_run_launches_app(monkeypatch, tmp_path):
    from flight import _viewer

    launched = {}
    monkeypatch.setattr(_viewer.FlightViewer, "run", lambda self: launched.setdefault("ok", True))
    _viewer.run(_alias_crash(tmp_path))  # line 194
    assert launched["ok"]
