"""Extended ecosystem + what-if coverage (Phase 9 / Phase 10).

A broad, parametrized companion to ``test_ecosystem.py`` / ``test_whatif.py`` /
``test_viewer_model.py``. Everything here is deterministic and framework-free:

* the naming helpers (`_safe_name`, `_safe_path_component`, `_flight_name`) are
  exercised over a wide table of node ids / paths / methods;
* the WSGI and ASGI middleware are driven with hand-built environ/scope objects
  (stdlib only) and asserted on **both** the black box *and* the unchanged
  response/exception behaviour (P1);
* `_ci.render_comment` and `_md_escape` are checked on real crash `.flight`s and
  ring-only snapshots;
* `_viewer_model` pure logic is checked over a synthesized crash;
* `what_if` (skipped below 3.13) covers every outcome kind.

Every case makes a meaningful assertion; the autouse ``_clean_flight`` fixture
(in ``conftest.py``) uninstalls Flight between tests.
"""

from __future__ import annotations

import asyncio
import inspect
import re
import sys
from pathlib import Path

import pytest

import flight
from flight import FlightASGI, FlightWSGI, Outcome, Override, WhatIf
from flight._ci import _md_escape, render_comment
from flight._pytest import _safe_name
from flight._web import (
    _IterGuard,
    _asgi_header,
    _context_from_traceparent,
    _flight_name,
    _safe_path_component,
)
from flight import _viewer_model as vm

_SAFE_RE = re.compile(r"\A[A-Za-z0-9_.-]*\Z")


# =========================================================================
# _safe_name  (pytest plugin)
# =========================================================================

# (nodeid, expected)
_SAFE_NAME_EXACT = [
    ("tests/test_x.py::test_foo[a-b]", "tests_test_x.py_test_foo_a-b"),
    ("", "test"),
    ("///", "test"),
    ("!!!", "test"),
    ("a b c", "a_b_c"),
    ("test_simple", "test_simple"),
    ("pkg/mod.py::Test::test_m", "pkg_mod.py_Test_test_m"),
    ("weird!!!name", "weird_name"),
    ("with spaces and/slashes", "with_spaces_and_slashes"),
    ("[params]", "params"),
    ("__dunder__", "dunder"),
    ("café", "caf"),
    ("123", "123"),
    ("a.b.c", "a.b.c"),
    ("-dash-", "-dash-"),
    ("multi___under", "multi___under"),
    ("t::test[x=1|y=2]", "t_test_x_1_y_2"),
    ("path/to/test.py::test_it[0]", "path_to_test.py_test_it_0"),
    ("已经::test", "test"),
    ("mix_of.Things-123", "mix_of.Things-123"),
]


@pytest.mark.parametrize("nodeid, expected", _SAFE_NAME_EXACT)
def test_safe_name_exact(nodeid, expected):
    got = _safe_name(nodeid)
    assert got == expected
    assert _SAFE_RE.match(got), f"{got!r} is not filesystem-safe"
    assert not got.startswith("_") and not got.endswith("_")


# (nodeid, max_len) — length cap + distinctive tail preserved
_SAFE_NAME_LEN = [
    ("a/" * 200 + "test_z", 120, "test_z"),
    ("x" * 200, 120, "x"),
    ("dir/" * 100 + "test_tail", 120, "test_tail"),
    ("q" * 300, 50, "q"),
    ("segment_" * 40 + "final_name", 60, "final_name"),
]


@pytest.mark.parametrize("nodeid, max_len, tail", _SAFE_NAME_LEN)
def test_safe_name_length_cap(nodeid, max_len, tail):
    got = _safe_name(nodeid, max_len=max_len)
    assert len(got) <= max_len
    assert got.endswith(tail)
    assert _SAFE_RE.match(got)
    assert not got.startswith("_")


@pytest.mark.parametrize(
    "nodeid",
    [
        "tests/test_a.py::test_1",
        "a/b/c::test",
        "weird chars *&^%$",
        "[only-params]",
        "trailing___",
        "___leading",
        "",
        "normal_name.py",
        "已经全部非法",
        "mix 123 / abc",
    ],
)
def test_safe_name_is_always_safe_and_nonempty(nodeid):
    got = _safe_name(nodeid)
    assert _SAFE_RE.match(got)
    assert got  # never empty (falls back to "test")
    assert not got.startswith("_") and not got.endswith("_")


# =========================================================================
# _safe_path_component  (web)
# =========================================================================

_SAFE_PATH_EXACT = [
    ("/orders/42", "orders_42"),
    ("", "root"),
    ("/", "root"),
    ("///", "root"),
    ("!!!", "root"),
    ("/api/v1/users", "api_v1_users"),
    ("simple", "simple"),
    ("a.b-c_d", "a.b-c_d"),
    ("with space", "with_space"),
    ("hello!world", "hello_world"),
    ("café", "café"),  # é is str.isalnum() → kept (unlike the regex in _safe_name)
    ("_leading", "leading"),
    ("trailing_", "trailing"),
    ("/path/with/many/segments", "path_with_many_segments"),
    ("/users/123/orders/456", "users_123_orders_456"),
    ("query?a=1&b=2", "query_a_1_b_2"),
    ("dots...ok", "dots...ok"),
    ("dash-ok", "dash-ok"),
    ("MixedCASE", "MixedCASE"),
    ("/checkout", "checkout"),
]


@pytest.mark.parametrize("path, expected", _SAFE_PATH_EXACT)
def test_safe_path_component_exact(path, expected):
    got = _safe_path_component(path)
    assert got == expected
    # only alnum plus the allowed set survive
    assert all(c.isalnum() or c in "-._" for c in got)
    assert got  # never empty (falls back to "root")


@pytest.mark.parametrize(
    "path",
    [
        "a" * 100,
        "/" + "seg/" * 100,
        "x/" * 100,
        "长" * 100,
        "!" * 100,
        "mix" * 50,
    ],
)
def test_safe_path_component_length_cap(path):
    got = _safe_path_component(path)
    assert len(got) <= 60
    assert got  # never empty


# =========================================================================
# _flight_name  (web)
# =========================================================================


@pytest.mark.parametrize(
    "method, path",
    [
        ("get", "/orders/42"),
        ("POST", "/checkout"),
        ("Put", "/users/1"),
        ("delete", "/a/b/c"),
        ("patch", "/x"),
        ("options", "/"),
        ("head", ""),
        ("GET", "!!!"),
        ("post", "/very/long/" + "seg/" * 40),
        ("get", "café"),
        ("trace", "/query?a=1"),
        ("connect", "/nested/path/here"),
    ],
)
def test_flight_name_shape(method, path):
    name = _flight_name(method, path)
    assert name.startswith(f"http-{method.upper()}-")
    assert name.endswith(".flight")
    # the sanitized path component is embedded (unless the whole thing is long)
    comp = _safe_path_component(path)
    assert comp in name
    # a millisecond timestamp sits before the extension
    stamp = name[: -len(".flight")].rsplit("-", 1)[-1]
    assert stamp.isdigit() and int(stamp) > 0


def test_flight_names_are_time_ordered_unique_enough():
    import time

    a = _flight_name("GET", "/x")
    time.sleep(0.002)
    b = _flight_name("GET", "/x")
    assert a != b  # the timestamp advances


# =========================================================================
# _context_from_traceparent + _asgi_header  (web, pure)
# =========================================================================

_VALID_TP = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"


@pytest.mark.parametrize(
    "tp, expected_trace_id",
    [
        (None, None),
        ("", None),
        ("garbage", None),
        ("00-tooshort-abc-01", None),
        (_VALID_TP, "4bf92f3577b34da6a3ce929d0e0e4736"),
        (
            "00-11111111111111111111111111111111-2222222222222222-01",
            "1" * 32,
        ),
        ("00-" + "0" * 32 + "-" + "1" * 16 + "-01", None),  # all-zero trace id rejected
    ],
)
def test_context_from_traceparent(tp, expected_trace_id):
    ctx = _context_from_traceparent(tp, service="svc")
    if expected_trace_id is None:
        assert ctx is None
    else:
        assert ctx is not None
        assert ctx.trace_id == expected_trace_id
        assert ctx.service == "svc"


@pytest.mark.parametrize(
    "headers, name, expected",
    [
        ([(b"traceparent", b"abc")], b"traceparent", "abc"),
        ([(b"Traceparent", b"XYZ")], b"traceparent", "XYZ"),  # case-insensitive key
        ([(b"content-type", b"text/html")], b"traceparent", None),
        ([], b"traceparent", None),
        ([(b"a", b"1"), (b"traceparent", b"tp"), (b"b", b"2")], b"traceparent", "tp"),
        ([(b"x-custom", b"v")], b"x-custom", "v"),
        ([(b"traceparent", b"\xff\xfe")], b"traceparent", "\xff\xfe"),  # latin-1 decode
    ],
)
def test_asgi_header_extraction(headers, name, expected):
    scope = {"headers": headers}
    assert _asgi_header(scope, name) == expected


def test_asgi_header_missing_headers_key():
    assert _asgi_header({}, b"traceparent") is None
    assert _asgi_header({"headers": None}, b"traceparent") is None


# =========================================================================
# _md_escape  (ci)
# =========================================================================


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("a|b", "a\\|b"),
        ("x\ny", "x y"),
        ("  hi  ", "hi"),
        ("plain", "plain"),
        ("|start", "\\|start"),
        ("end|", "end\\|"),
        ("a\nb\nc", "a b c"),
        ("\n\n", ""),
        ("mix|\n", "mix\\|"),
        ("   ", ""),
        ("no|pipes|here", "no\\|pipes\\|here"),
        ("a", "a"),
        ("café|", "café\\|"),
        ("a|b\nc|d", "a\\|b c\\|d"),
        ("tab\ttab", "tab\ttab"),
    ],
)
def test_md_escape(raw, expected):
    assert _md_escape(raw) == expected
    # invariants: no raw newline survives, no bare pipe survives
    out = _md_escape(raw)
    assert "\n" not in out
    assert "|" not in out.replace("\\|", "")


# =========================================================================
# WSGI middleware  (stdlib-only; assert black box AND response/exception)
# =========================================================================


def _make_raising_app(exc: Exception):
    def app(environ, start_response):
        payload = {"k": 1}  # noqa: F841 — captured local
        raise exc

    return app


@pytest.mark.parametrize(
    "exc, exc_name",
    [
        (IndexError("idx"), "IndexError"),
        (KeyError("missing"), "KeyError"),
        (ValueError("bad"), "ValueError"),
        (ZeroDivisionError("div"), "ZeroDivisionError"),
        (RuntimeError("boom"), "RuntimeError"),
        (TypeError("type"), "TypeError"),
    ],
)
def test_wsgi_raising_app_writes_flight_and_reraises(tmp_path, exc, exc_name):
    out = tmp_path / "w"
    wrapped = FlightWSGI(_make_raising_app(exc), output_dir=out, service="svc")
    env = {"REQUEST_METHOD": "GET", "PATH_INFO": "/orders/1"}
    try:
        with pytest.raises(type(exc)):  # P1: exception unchanged, still propagates
            wrapped(env, lambda s, h: None)
    finally:
        flight.uninstall()
    files = list(out.glob("*.flight"))
    assert len(files) == 1  # exactly one black box for the failure
    f = flight.read(files[0])
    assert f.has_crash
    assert f.exceptions[0][0] == exc_name


@pytest.mark.parametrize(
    "method, path, body",
    [
        ("GET", "/", b"hello"),
        ("POST", "/checkout", b"ok"),
        ("GET", "/health", b""),
        ("PUT", "/users/9", b"updated"),
    ],
)
def test_wsgi_success_passes_through_no_flight(tmp_path, method, path, body):
    out = tmp_path / "w"

    def app(environ, start_response):
        start_response("200 OK", [])
        return [body]

    wrapped = FlightWSGI(app, output_dir=out)
    try:
        got = b"".join(wrapped({"REQUEST_METHOD": method, "PATH_INFO": path}, lambda s, h: None))
    finally:
        flight.uninstall()
    assert got == body  # response unchanged
    assert not list(out.glob("*.flight"))  # no crash → no black box


@pytest.mark.parametrize(
    "exc, exc_name",
    [
        (RuntimeError("stream broke"), "RuntimeError"),
        (ValueError("mid-body"), "ValueError"),
        (IndexError("late"), "IndexError"),
        (KeyError("gen"), "KeyError"),
        (ZeroDivisionError("z"), "ZeroDivisionError"),
    ],
)
def test_wsgi_body_iteration_error_is_captured(tmp_path, exc, exc_name):
    out = tmp_path / "w"

    def app(environ, start_response):
        start_response("200 OK", [])

        def body():
            yield b"partial"
            raise exc  # error while the server iterates the response

        return body()

    wrapped = FlightWSGI(app, output_dir=out)
    result = wrapped({"REQUEST_METHOD": "GET", "PATH_INFO": "/stream"}, lambda s, h: None)
    try:
        with pytest.raises(type(exc)):
            list(result)  # driving iteration surfaces the error, unchanged
    finally:
        flight.uninstall()
    files = list(out.glob("*.flight"))
    assert len(files) == 1
    assert flight.read(files[0]).exceptions[0][0] == exc_name


@pytest.mark.parametrize(
    "tp, expected_trace_id",
    [
        (_VALID_TP, "4bf92f3577b34da6a3ce929d0e0e4736"),
        ("00-11111111111111111111111111111111-2222222222222222-01", "1" * 32),
        ("00-abcdefabcdefabcdefabcdefabcdefab-1234567812345678-01", "abcdefabcdefabcdefabcdefabcdefab"),
    ],
)
def test_wsgi_traceparent_lands_on_black_box(tmp_path, tp, expected_trace_id):
    out = tmp_path / "w"
    wrapped = FlightWSGI(_make_raising_app(ValueError("x")), output_dir=out, service="checkout")
    env = {"REQUEST_METHOD": "GET", "PATH_INFO": "/o", "HTTP_TRACEPARENT": tp}
    try:
        with pytest.raises(ValueError):
            wrapped(env, lambda s, h: None)
    finally:
        flight.uninstall()
    files = list(out.glob("*.flight"))
    assert len(files) == 1
    # per-request correlation rode onto THIS request's black box (not global)
    assert flight.read(files[0]).trace_id == expected_trace_id


def test_wsgi_no_traceparent_means_no_trace_id(tmp_path):
    out = tmp_path / "w"
    wrapped = FlightWSGI(_make_raising_app(ValueError("x")), output_dir=out)
    try:
        with pytest.raises(ValueError):
            wrapped({"REQUEST_METHOD": "GET", "PATH_INFO": "/o"}, lambda s, h: None)
    finally:
        flight.uninstall()
    f = flight.read(list(out.glob("*.flight"))[0])
    assert f.trace_id is None


# -- _IterGuard unit behaviour (no Flight install needed) -------------------


class _Closable:
    def __init__(self, items, boom=None):
        self._items = list(items)
        self.boom = boom
        self.closed = False

    def __iter__(self):
        for x in self._items:
            yield x
        if self.boom is not None:
            raise self.boom

    def close(self):
        self.closed = True


@pytest.mark.parametrize(
    "items",
    [[], [b"a"], [b"a", b"b", b"c"], [b"x"] * 10],
)
def test_iterguard_passes_through_normal_iteration(items):
    calls = []
    guard = _IterGuard(iter(items), lambda: calls.append(1))
    assert list(guard) == items
    assert calls == []  # on_error never fires on clean exhaustion


@pytest.mark.parametrize(
    "exc",
    [RuntimeError("x"), ValueError("y"), IndexError("z")],
)
def test_iterguard_fires_on_error_and_reraises(exc):
    calls = []

    def gen():
        yield b"one"
        raise exc

    guard = _IterGuard(gen(), lambda: calls.append(1))
    collected = []
    with pytest.raises(type(exc)):
        for chunk in guard:
            collected.append(chunk)
    assert collected == [b"one"]  # earlier chunks delivered unchanged
    assert calls == [1]  # captured exactly once


@pytest.mark.parametrize("items", [[], [b"a"], [b"a", b"b"]])
def test_iterguard_forwards_close(items):
    inner = _Closable(items)
    guard = _IterGuard(inner, lambda: None)
    assert list(guard) == items
    assert not inner.closed
    guard.close()
    assert inner.closed  # server's resource handling preserved


def test_iterguard_close_is_safe_without_inner_close():
    guard = _IterGuard(iter([b"a"]), lambda: None)
    # plain iterator has no .close — must not raise
    guard.close()


# =========================================================================
# ASGI middleware  (asyncio; assert black box AND behaviour)
# =========================================================================


def _make_async_raising_app(exc: Exception):
    async def app(scope, receive, send):
        state = {"n": 1}  # noqa: F841
        raise exc

    return app


async def _noop_receive():
    return {"type": "http.request"}


async def _noop_send(_message):
    return None


@pytest.mark.parametrize(
    "exc, exc_name",
    [
        (KeyError("missing"), "KeyError"),
        (ValueError("bad"), "ValueError"),
        (RuntimeError("boom"), "RuntimeError"),
        (IndexError("idx"), "IndexError"),
        (ZeroDivisionError("z"), "ZeroDivisionError"),
    ],
)
def test_asgi_raising_app_writes_flight_and_reraises(tmp_path, exc, exc_name):
    out = tmp_path / "a"
    wrapped = FlightASGI(_make_async_raising_app(exc), output_dir=out, service="api")
    scope = {"type": "http", "method": "POST", "path": "/checkout", "headers": []}
    try:
        with pytest.raises(type(exc)):
            asyncio.run(wrapped(scope, _noop_receive, _noop_send))
    finally:
        flight.uninstall()
    files = list(out.glob("*.flight"))
    assert len(files) == 1
    assert flight.read(files[0]).exceptions[0][0] == exc_name


@pytest.mark.parametrize("scope_type", ["lifespan", "websocket", "unknown"])
def test_asgi_non_http_scopes_pass_through_untouched(tmp_path, scope_type):
    out = tmp_path / "a"
    seen = []

    async def app(scope, receive, send):
        seen.append(scope["type"])

    wrapped = FlightASGI(app, output_dir=out)
    try:
        asyncio.run(wrapped({"type": scope_type}, None, None))
    finally:
        flight.uninstall()
    assert seen == [scope_type]  # app invoked, untouched
    assert not list(out.glob("*.flight"))  # no black box for non-http


@pytest.mark.parametrize(
    "tp, expected_trace_id",
    [
        ("00-11111111111111111111111111111111-2222222222222222-01", "1" * 32),
        (_VALID_TP, "4bf92f3577b34da6a3ce929d0e0e4736"),
        ("00-abcdefabcdefabcdefabcdefabcdefab-1234567812345678-01", "abcdefabcdefabcdefabcdefabcdefab"),
    ],
)
def test_asgi_traceparent_from_scope_headers(tmp_path, tp, expected_trace_id):
    out = tmp_path / "a"
    wrapped = FlightASGI(_make_async_raising_app(ValueError("x")), output_dir=out, service="api")
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/x",
        "headers": [(b"traceparent", tp.encode("latin-1"))],
    }
    try:
        with pytest.raises(ValueError):
            asyncio.run(wrapped(scope, _noop_receive, _noop_send))
    finally:
        flight.uninstall()
    f = flight.read(list(out.glob("*.flight"))[0])
    assert f.trace_id == expected_trace_id


# =========================================================================
# _ci.render_comment
# =========================================================================


def _crash_flight(path: Path, fn):
    """Record a crash `.flight` by running `fn` (which raises) under Flight."""
    flight.install(output_dir=path.parent)
    try:
        fn()
    except Exception:
        flight.capture(path=str(path))
    flight.uninstall()
    return path


def _div_zero():
    def average(nums):
        return sum(nums) / len(nums)

    return average([])


def _index_err():
    def pick(rows):
        return rows[99]

    return pick([1, 2, 3])


def _key_err():
    def lookup(d):
        return d["absent"]

    return lookup({})


def _value_err():
    def parse(s):
        return int(s)

    return parse("not-a-number")


@pytest.mark.parametrize(
    "fn, exc_name, frame_name",
    [
        (_div_zero, "ZeroDivisionError", "average"),
        (_index_err, "IndexError", "pick"),
        (_key_err, "KeyError", "lookup"),
        (_value_err, "ValueError", "parse"),
    ],
)
def test_render_comment_markdown(tmp_path, fn, exc_name, frame_name):
    src = _crash_flight(tmp_path / "c.flight", fn)
    md = render_comment(src)
    assert md.startswith("### ")  # a heading, drop-in safe
    assert exc_name in md
    assert "Fingerprint" in md
    assert "<details>" in md
    assert frame_name in md  # the crash frame's function


@pytest.mark.parametrize("title", ["Flight — root cause", "CI failure", "Boom|pipe"])
def test_render_comment_uses_title(tmp_path, title):
    src = _crash_flight(tmp_path / "c.flight", _div_zero)
    md = render_comment(src, title=title)
    assert md.splitlines()[0].startswith("### ")
    assert title in md.splitlines()[0]


@pytest.mark.parametrize("repro_hint", [True, False])
def test_render_comment_repro_hint_toggle(tmp_path, repro_hint):
    src = _crash_flight(tmp_path / "c.flight", _div_zero)
    md = render_comment(src, repro_hint=repro_hint)
    assert ("Open this black box" in md) is repro_hint


def test_render_comment_ring_only_has_no_crash_detail(tmp_path):
    flight.install(output_dir=tmp_path)
    src = flight.dump(tmp_path / "ring.flight")
    flight.uninstall()
    md = render_comment(src)
    assert "no crash detail" in md
    assert "Fingerprint" not in md  # nothing to fingerprint on a ring-only snapshot


# =========================================================================
# _viewer_model  (pure logic over a synthesized crash)
# =========================================================================


@pytest.fixture
def crash(tmp_path):
    out = tmp_path / "c.flight"
    flight.install()

    def inner(cfg):
        scale = 2  # noqa: F841
        raise ValueError("boom")

    def outer():
        config = {"mode": "prod", "retries": 3}
        password = "s3cret"  # noqa: F841
        inner(config)

    try:
        outer()
    except ValueError:
        flight.capture(path=out)
    flight.uninstall()
    return flight.read(out).crash()


def _find(crash, qualname):
    for i, fr in enumerate(crash.frames):
        if fr.qualname.endswith(qualname):
            return i
    raise AssertionError(qualname)


@pytest.mark.parametrize(
    "line_text, present, absent",
    [
        ("scale + cfg", {"scale", "cfg"}, set()),
        ("cfg cfg cfg", {"cfg"}, set()),
        ("scale = other_var", {"scale"}, {"other_var"}),
        ("", set(), {"scale", "cfg"}),
        ("nothing_here = 1", set(), {"scale", "cfg"}),
        ("return scale", {"scale"}, set()),
        ("1 + 2 + 3", set(), {"scale"}),
        ("cfg.get(scale)", {"cfg", "scale"}, set()),
        ("result = scale2 + scaled", set(), {"scale", "cfg"}),
    ],
)
def test_inline_values(crash, line_text, present, absent):
    i = _find(crash, "inner")
    locs = vm.frame_locals(crash, i)
    vals = vm.inline_values(line_text, locs)
    names = [n for n, _v in vals]
    assert set(names) >= present
    assert not (set(names) & absent)
    assert len(names) == len(set(names))  # de-duplicated


def test_inline_values_first_appearance_order(crash):
    i = _find(crash, "inner")
    locs = vm.frame_locals(crash, i)
    names = [n for n, _v in vm.inline_values("cfg then scale then cfg", locs)]
    assert names == ["cfg", "scale"]  # order of first appearance


@pytest.mark.parametrize("context", [0, 1, 2, 3, 5, 8, 50])
def test_source_window_bounds(crash, context):
    i = _find(crash, "inner")
    rows, cur = vm.source_window(crash, i, context=context)
    assert rows, "inner's source should be captured"
    linenos = [n for n, _t, _v in rows]
    assert cur in linenos
    assert min(linenos) >= max(1, cur - context)
    assert max(linenos) <= cur + context
    assert linenos == sorted(linenos)
    for row in rows:
        assert len(row) == 3  # (lineno, text, [(name, value), ...])


def test_source_window_has_inline_annotations_somewhere(crash):
    i = _find(crash, "inner")
    rows, _cur = vm.source_window(crash, i, context=6)
    assert any(vals for _n, _t, vals in rows)


@pytest.mark.parametrize("qualname, local_name", [("outer", "config")])
def test_object_detail_and_children(crash, qualname, local_name):
    idx = _find(crash, qualname)
    oid = dict(crash.frames[idx].locals)[local_name]
    detail = "\n".join(vm.object_detail(crash, oid))
    assert "kind" in detail
    assert vm.has_children(crash, oid)
    kids = dict(vm.object_children(crash, oid))
    assert "mode" in kids and "retries" in kids
    label = vm.object_label(crash, oid, key=local_name)
    assert label.startswith(f"{local_name} = ")


def test_object_detail_missing_node(crash):
    lines = vm.object_detail(crash, 10_000_000)
    assert lines == ["<missing object #10000000>"]
    assert vm.object_label(crash, 10_000_000) == "<missing #10000000>"
    assert not vm.has_children(crash, 10_000_000)
    assert vm.object_children(crash, 10_000_000) == []


def test_alias_index_shared_object_excludes_scalars(crash):
    ci = _find(crash, "inner")
    co = _find(crash, "outer")
    cfg_id = dict(crash.frames[ci].locals)["cfg"]
    config_id = dict(crash.frames[co].locals)["config"]
    assert cfg_id == config_id  # same dict passed down
    aliases = vm.alias_index(crash)
    assert cfg_id in aliases
    names = {name for _i, name in aliases[cfg_id]}
    assert {"cfg", "config"} <= names
    # the shared object's detail reports the aliasing
    assert "aliased" in "\n".join(vm.object_detail(crash, cfg_id))


def test_scrubbed_local_is_redacted(crash):
    co = _find(crash, "outer")
    locs = vm.frame_locals(crash, co)
    assert locs["password"][1] == "<redacted>"


# =========================================================================
# what_if  (Phase 10) — skipped below 3.13
# =========================================================================

requires_313 = pytest.mark.skipif(
    sys.version_info < (3, 13),
    reason="what-if needs PEP 667 write-through locals (3.13+)",
)


def _line(fn, marker: str) -> int:
    src, start = inspect.getsourcelines(fn)
    for i, line in enumerate(src):
        if marker in line:
            return start + i
    raise AssertionError(f"marker {marker!r} not found in {fn.__name__}")


# functions under test (module level so replay can resolve them)
def crashing():
    import random

    data = []
    factor = random.randint(100, 999)  # recorded on the tape
    return factor * (sum(data) / len(data))  # WHATIF_USE (ZeroDivision when empty)


def branch():
    import random

    flag = False
    if flag:  # WHATIF_FLAG
        random.random()  # only if flag is True — not on the tape
    return random.randint(0, 9)  # recorded on the tape


def pure():
    x = 5
    return x * 2  # WHATIF_PURE (no non-determinism)


def _record(path, fn):
    try:
        with flight.deterministic(str(path)):
            fn()
    except Exception:
        pass
    return str(path)


# -- pure Outcome / Override / WhatIf logic (no tape needed) ---------------


@pytest.mark.parametrize(
    "outcome, exp_raised, exp_key0, describe_sub",
    [
        (Outcome(returned=5), False, "returned", "returned 5"),
        (Outcome(returned=None), False, "returned", "returned None"),
        (Outcome(exception=ValueError("x")), True, "raised", "raised ValueError"),
        (Outcome(exception=KeyError("k")), True, "raised", "raised KeyError"),
        (Outcome(diverged=True), False, "diverged", "diverged from the recorded run"),
        # diverged wins over a stashed exception: not "raised"
        (Outcome(exception=RuntimeError("r"), diverged=True), False, "diverged", "diverged"),
    ],
)
def test_outcome_key_raised_describe(outcome, exp_raised, exp_key0, describe_sub):
    assert outcome.raised is exp_raised
    assert outcome.key()[0] == exp_key0
    assert describe_sub in outcome.describe()


@pytest.mark.parametrize(
    "a, b, changed",
    [
        (Outcome(returned=1), Outcome(returned=1), False),
        (Outcome(returned=1), Outcome(returned=2), True),
        (Outcome(returned=1), Outcome(exception=ValueError("x")), True),
        (Outcome(exception=ValueError("x")), Outcome(exception=ValueError("x")), False),
        (Outcome(exception=ValueError("x")), Outcome(exception=ValueError("y")), True),
        (Outcome(returned=1), Outcome(diverged=True), True),
        (Outcome(diverged=True), Outcome(diverged=True), False),
    ],
)
def test_whatif_changed_property(a, b, changed):
    wi = WhatIf(baseline=a, counterfactual=b, overrides=[])
    assert wi.changed is changed


def test_whatif_unreached_lists_unfired_overrides():
    o1 = Override("x", 1, line=1)
    o2 = Override("y", 2, line=2)
    o1.applied = True
    o2.applied = False
    wi = WhatIf(baseline=Outcome(returned=0), counterfactual=Outcome(returned=0), overrides=[o1, o2])
    assert wi.unreached == [o2]


@pytest.mark.parametrize(
    "ov, sub",
    [
        (Override("data", [1, 2], line=42), "data :="),
        (Override("n", 5, line=10, qualname="mod.fn"), "mod.fn:10"),
        (Override("x", "v", line=7), "line 7"),
    ],
)
def test_override_describe(ov, sub):
    assert sub in ov.describe()


# -- end-to-end what-if over a real deterministic tape ---------------------


@requires_313
def test_whatif_change_fixes_crash(tmp_path):
    path = _record(tmp_path / "c.flight", crashing)
    wi = flight.what_if(path, crashing, Override("data", [2, 4], line=_line(crashing, "WHATIF_USE")))
    assert wi.baseline.raised and isinstance(wi.baseline.exception, ZeroDivisionError)
    assert not wi.counterfactual.raised
    assert wi.changed
    ov = wi.overrides[0]
    assert ov.applied
    assert ov.previous == "[]"  # the recorded value it replaced
    assert wi.counterfactual.returned % 3 == 0


@requires_313
def test_whatif_inert_change_no_effect(tmp_path):
    path = _record(tmp_path / "c.flight", crashing)
    wi = flight.what_if(path, crashing, Override("data", [], line=_line(crashing, "WHATIF_USE")))
    assert wi.overrides[0].applied  # override fired
    assert wi.counterfactual.raised and isinstance(wi.counterfactual.exception, ZeroDivisionError)
    assert not wi.changed


@requires_313
def test_whatif_override_never_reached(tmp_path):
    path = _record(tmp_path / "c.flight", crashing)
    wi = flight.what_if(path, crashing, Override("data", [9], line=999999))
    assert not wi.overrides[0].applied
    assert wi.unreached == wi.overrides
    assert not wi.changed
    assert wi.counterfactual.raised


@requires_313
def test_whatif_divergence(tmp_path):
    path = _record(tmp_path / "b.flight", branch)
    wi = flight.what_if(path, branch, Override("flag", True, line=_line(branch, "WHATIF_FLAG")))
    assert not wi.baseline.diverged
    assert wi.counterfactual.diverged
    assert wi.changed
    assert "diverged" in wi.counterfactual.describe()


@requires_313
def test_whatif_deterministic_function(tmp_path):
    path = _record(tmp_path / "p.flight", pure)
    wi = flight.what_if(path, pure, Override("x", 10, line=_line(pure, "WHATIF_PURE")))
    assert wi.baseline.returned == 10
    assert wi.counterfactual.returned == 20
    assert wi.changed
    assert wi.overrides[0].previous == "5"


@requires_313
def test_whatif_single_override_or_list_equivalent(tmp_path):
    path = _record(tmp_path / "c.flight", crashing)
    line = _line(crashing, "WHATIF_USE")
    one = flight.what_if(path, crashing, Override("data", [3], line=line))
    many = flight.what_if(path, crashing, [Override("data", [3], line=line)])
    assert one.counterfactual.returned == many.counterfactual.returned
    assert one.changed and many.changed


@requires_313
def test_whatif_holds_recorded_world_constant(tmp_path):
    path = _record(tmp_path / "c.flight", crashing)
    line = _line(crashing, "WHATIF_USE")
    a = flight.what_if(path, crashing, Override("data", [1, 1], line=line))
    b = flight.what_if(path, crashing, Override("data", [1, 1], line=line))
    assert a.counterfactual.returned == b.counterfactual.returned  # random from tape


@requires_313
@pytest.mark.parametrize(
    "sub",
    ["what-if:", "before:", "after:", "alters the outcome"],
)
def test_whatif_render_report(tmp_path, sub):
    path = _record(tmp_path / "c.flight", crashing)
    wi = flight.what_if(path, crashing, Override("data", [2, 4], line=_line(crashing, "WHATIF_USE")))
    assert sub in wi.render()


@requires_313
def test_whatif_render_no_change_branch(tmp_path):
    path = _record(tmp_path / "c.flight", crashing)
    wi = flight.what_if(path, crashing, Override("data", [], line=_line(crashing, "WHATIF_USE")))
    text = wi.render()
    assert "no change to the outcome" in text
