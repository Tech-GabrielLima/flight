"""Extended foundation suite: Config, Scrubber, GraphSerializer, adapters.

Exhaustive property/boundary tests for the Phase-1 crash-capture foundation.
Every case asserts real, observed behaviour (no filler). Parametrization is
used heavily; each parameter is a distinct, meaningful assertion.
"""

from __future__ import annotations

import math
import os
import sys
import types

import pytest

import flight
from flight._config import Config, _stdlib_and_site_prefixes
from flight._scrub import DEFAULT_PATTERNS, REDACTED, Scrubber
from flight._serialize import (
    MAX_CONTAINER,
    MAX_DEPTH,
    MAX_STR,
    REPR_LIMIT,
    GraphSerializer,
    describe_shallow,
)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _graph(roots, **kw):
    g = GraphSerializer(**kw)
    ids = [g.add_root(r) for r in roots]
    nodes = g.run()
    by_id = {n[0]: n for n in nodes}
    return g, ids, by_id


def _kind(node):
    return node[1]


def _repr(node):
    return node[2]


def _type_name(node):
    return node[3]


def _length(node):
    return node[4]


def _trunc(node):
    return node[5]


def _items(node):
    return node[6]


# ==========================================================================
# Config.is_interesting
# ==========================================================================

@pytest.mark.parametrize(
    "path",
    ["", "<string>", "<frozen importlib._bootstrap>", "<stdin>", "<unknown>", "<ast>"],
)
def test_is_interesting_synthetic_paths_false(path):
    # Empty or `<...>` synthetic code is never recorded.
    assert Config().is_interesting(path) is False


@pytest.mark.parametrize(
    "module",
    [os, sys, math, types],
)
def test_is_interesting_stdlib_denied(module):
    f = getattr(module, "__file__", None)
    if not f:  # some builtins have no file
        pytest.skip("module has no __file__")
    assert Config().is_interesting(f) is False


def test_is_interesting_flight_itself_denied():
    # Flight never records its own package code.
    assert Config().is_interesting(flight._config.__file__) is False


@pytest.mark.parametrize("name", ["app.py", "service/handler.py", "deep/nested/mod.py"])
def test_is_interesting_user_path_true(tmp_path, name):
    # A path outside every deny prefix is recorded.
    p = tmp_path / name
    assert Config().is_interesting(str(p)) is True


def test_is_interesting_empty_deny_records_everything():
    c = Config(deny_prefixes=())
    assert c.is_interesting("/anywhere/at/all/x.py") is True
    # ...but synthetic paths are still excluded regardless of deny list.
    assert c.is_interesting("<string>") is False
    assert c.is_interesting("") is False


def test_force_include_overrides_deny():
    c = Config(deny_prefixes=("/opt/pkg",), force_include=("special",))
    assert c.is_interesting("/opt/pkg/special/mod.py") is True   # force wins
    assert c.is_interesting("/opt/pkg/other/mod.py") is False     # denied


def test_force_include_checked_before_deny_even_for_stdlib():
    stdlib_dir = os.path.dirname(os.__file__)
    c = Config(force_include=(stdlib_dir,))
    assert c.is_interesting(os.__file__) is True


def test_is_interesting_relative_path_is_realpathed(tmp_path, monkeypatch):
    # A relative filename is resolved against cwd, so a cwd deny prefix bites.
    monkeypatch.chdir(tmp_path)
    c = Config(deny_prefixes=(os.path.realpath(str(tmp_path)),))
    assert c.is_interesting("foo.py") is False
    # And with no deny prefix, the same relative path is interesting.
    assert Config(deny_prefixes=()).is_interesting("foo.py") is True


def test_is_interesting_substring_not_prefix_for_deny():
    # deny is a prefix match, not a substring match.
    c = Config(deny_prefixes=("/opt/pkg",))
    assert c.is_interesting("/other/opt/pkg/x.py") is True  # deny not a prefix


# ==========================================================================
# Config crash_path / scope_path
# ==========================================================================

@pytest.mark.parametrize(
    "pid,when_ms",
    [(1, 0), (123, 456), (99999, 1_700_000_000_000), (0, 0), (7, 8)],
)
def test_crash_path_naming(tmp_path, pid, when_ms):
    c = Config(output_dir=tmp_path)
    p = c.crash_path(pid, when_ms)
    assert p.name == f"flight-{pid}-{when_ms}.flight"
    assert p.parent == tmp_path


@pytest.mark.parametrize(
    "pid,when_ms",
    [(1, 0), (123, 456), (99999, 1_700_000_000_000), (0, 0), (7, 8)],
)
def test_scope_path_naming(tmp_path, pid, when_ms):
    c = Config(output_dir=tmp_path)
    p = c.scope_path(pid, when_ms)
    assert p.name == f"flight-scope-{pid}-{when_ms}.flight"
    assert p.parent == tmp_path


def test_crash_and_scope_paths_differ():
    c = Config()
    assert c.crash_path(1, 2) != c.scope_path(1, 2)


# ==========================================================================
# Config default fields / overrides
# ==========================================================================

@pytest.mark.parametrize(
    "attr,expected",
    [
        ("ring_capacity", 4096),
        ("dump_on_crash", True),
        ("record_lines", False),
        ("record_returns", True),
        ("capture_deadline_ms", 250),
        ("capture_max_bytes", 20 * 1024 * 1024),
        ("max_str", 10 * 1024),
        ("max_container", 200),
        ("max_depth", 6),
        ("repr_limit", 200),
        ("scrub_patterns", ()),
        ("capture_max_mutations", 200_000),
        ("force_include", ()),
        ("overhead_slo", None),
        ("governor_interval", 0.5),
        ("per_event_ns", 65.0),
        ("daemon", False),
        ("daemon_interval", 1.0),
        ("correlation", None),
    ],
)
def test_config_default_field_values(attr, expected):
    assert getattr(Config(), attr) == expected


def test_config_default_output_dir_is_cwd():
    from pathlib import Path

    assert Config().output_dir == Path.cwd()


def test_config_default_deny_prefixes_nonempty_and_absolute():
    dp = Config().deny_prefixes
    assert isinstance(dp, tuple)
    assert len(dp) >= 1
    assert all(os.path.isabs(p) for p in dp)


@pytest.mark.parametrize(
    "attr,value",
    [
        ("ring_capacity", 128),
        ("dump_on_crash", False),
        ("record_lines", True),
        ("record_returns", False),
        ("max_str", 5),
        ("max_container", 3),
        ("max_depth", 1),
        ("repr_limit", 12),
        ("overhead_slo", 0.03),
        ("daemon", True),
    ],
)
def test_config_field_override(attr, value):
    assert getattr(Config(**{attr: value}), attr) == value


# ==========================================================================
# _stdlib_and_site_prefixes
# ==========================================================================

def test_stdlib_and_site_prefixes_shape():
    prefixes = _stdlib_and_site_prefixes()
    assert isinstance(prefixes, tuple)
    assert all(isinstance(p, str) and p for p in prefixes)          # no empties
    assert all(p == os.path.realpath(p) for p in prefixes)          # realpathed
    assert list(prefixes) == sorted(prefixes)                        # sorted


def test_stdlib_and_site_prefixes_includes_flight_package_dir():
    flight_dir = os.path.realpath(os.path.dirname(flight._config.__file__))
    assert flight_dir in _stdlib_and_site_prefixes()


def test_stdlib_and_site_prefixes_includes_stdlib():
    prefixes = _stdlib_and_site_prefixes()
    assert any(os.__file__.startswith(p) for p in prefixes)


# ==========================================================================
# Scrubber
# ==========================================================================

@pytest.mark.parametrize("pat", DEFAULT_PATTERNS)
def test_default_patterns_exact_name_redacted(pat):
    assert Scrubber().should_redact(pat) is True


@pytest.mark.parametrize(
    "name",
    [
        "PASSWORD", "Password", "PassWord",
        "TOKEN", "Token", "ApiKey", "API_KEY", "Api_Key",
        "SECRET", "Secret", "AUTHORIZATION", "Cookie", "SSN", "CVV",
    ],
)
def test_scrubber_case_insensitive(name):
    assert Scrubber().should_redact(name) is True


@pytest.mark.parametrize(
    "name",
    [
        "user_password", "password_hash", "db_passwd",
        "auth_token", "access_token", "refresh_token",
        "my_secret_value", "the_api_key_here", "session_id",
        "http_cookie", "userAuth", "client_secret",
        "aws_access_key", "rsa_private_key", "user_credential",
        "card_number_masked", "the_cvv", "patient_ssn",
    ],
)
def test_scrubber_substring_match_redacts(name):
    assert Scrubber().should_redact(name) is True


@pytest.mark.parametrize(
    "name",
    [
        "username", "host", "port", "count", "index", "value",
        "result", "data", "items", "total", "status", "email",
        "first_name", "last_name", "amount", "quantity",
    ],
)
def test_scrubber_benign_names_pass_through(name):
    assert Scrubber().should_redact(name) is False


@pytest.mark.parametrize(
    "name",
    ["x-api-key", "API-KEY", "Api-Key"],
)
def test_scrubber_hyphenated_apikey_not_matched(name):
    # Only "api_key" (underscore) and "apikey" are patterns; hyphen variants
    # fall through — documents that separator characters matter.
    assert Scrubber().should_redact(name) is False


@pytest.mark.parametrize(
    "not_a_name",
    [None, 42, 3.14, b"password", ["password"], ("token",), object(), True],
)
def test_scrubber_non_string_returns_false(not_a_name):
    assert Scrubber().should_redact(not_a_name) is False


def test_scrubber_empty_name_false():
    assert Scrubber().should_redact("") is False


def test_scrubber_empty_patterns_never_redacts():
    s = Scrubber(patterns=())
    assert s.should_redact("password") is False
    assert s.should_redact("token") is False
    assert s.should_redact(42) is False


@pytest.mark.parametrize("name,expected", [
    ("magic_word", True),
    ("has_zzz_here", True),
    ("password", False),   # defaults are NOT included when custom patterns given
    ("token", False),
    ("plain", False),
])
def test_scrubber_custom_patterns_replace_defaults(name, expected):
    s = Scrubber(patterns=("magic", "zzz"))
    assert s.should_redact(name) is expected


def test_scrubber_extra_patterns_via_union():
    # A caller who wants defaults + extras passes the union explicitly.
    s = Scrubber(patterns=DEFAULT_PATTERNS + ("iban", "pin_code"))
    assert s.should_redact("password") is True
    assert s.should_redact("user_iban") is True
    assert s.should_redact("pin_code") is True


def test_scrubber_over_redacts_by_design():
    # Substring match with no word boundaries: "author" contains "auth", so it
    # redacts. This is intentional (P5) — the docstring documents it as such:
    # a false positive hides a value, a false negative leaks one.
    assert Scrubber().should_redact("author") is True
    assert Scrubber().should_redact("auth_token") is True
    assert Scrubber().should_redact("userAuth") is True


# ==========================================================================
# GraphSerializer — primitives
# ==========================================================================

@pytest.mark.parametrize(
    "value,kind,rep",
    [
        (None, "none", "None"),
        (True, "bool", "True"),
        (False, "bool", "False"),
        (0, "int", "0"),
        (42, "int", "42"),
        (-17, "int", "-17"),
        (2 ** 70, "int", str(2 ** 70)),
        (3.5, "float", "3.5"),
        (-0.0, "float", "-0.0"),
        (0.1, "float", "0.1"),
        ("", "str", ""),
        ("hello", "str", "hello"),
        (b"xy", "bytes", "b'xy'"),
        (b"", "bytes", "b''"),
    ],
)
def test_primitive_kind_and_repr(value, kind, rep):
    _, ids, by_id = _graph([value])
    node = by_id[ids[0]]
    assert _kind(node) == kind
    assert _repr(node) == rep
    assert _items(node) == []


def test_bool_is_not_int_even_though_subclass():
    _, ids, by_id = _graph([True, 1])
    assert _kind(by_id[ids[0]]) == "bool"
    assert _kind(by_id[ids[1]]) == "int"


@pytest.mark.parametrize(
    "value,rep",
    [
        (float("inf"), "inf"),
        (float("-inf"), "-inf"),
        (float("nan"), "nan"),
    ],
)
def test_float_special_values(value, rep):
    _, ids, by_id = _graph([value])
    node = by_id[ids[0]]
    assert _kind(node) == "float"
    assert _repr(node) == rep


def test_huge_int_repr_is_bit_length_summary():
    _, ids, by_id = _graph([10 ** 5000])
    node = by_id[ids[0]]
    assert _kind(node) == "int"
    assert node[2].startswith("<int ") and node[2].endswith("bits>")


def test_str_length_recorded():
    _, ids, by_id = _graph(["hello"])
    assert _length(by_id[ids[0]]) == 5


@pytest.mark.parametrize("n,truncated", [(9, False), (10, False), (11, True), (50, True)])
def test_str_truncation_boundary(n, truncated):
    _, ids, by_id = _graph(["x" * n], max_str=10)
    node = by_id[ids[0]]
    assert _length(node) == n              # real length always preserved
    assert _trunc(node) is truncated
    assert len(_repr(node)) == min(n, 10)  # rendered slice capped at max_str


@pytest.mark.parametrize("n,truncated", [(63, False), (64, False), (65, True), (200, True)])
def test_bytes_truncation_boundary(n, truncated):
    _, ids, by_id = _graph([b"a" * n])
    node = by_id[ids[0]]
    assert _kind(node) == "bytes"
    assert _length(node) == n
    assert _trunc(node) is truncated


def test_bytearray_serialized_as_bytes():
    _, ids, by_id = _graph([bytearray(b"abc")])
    node = by_id[ids[0]]
    assert _kind(node) == "bytes"
    assert _length(node) == 3


# ==========================================================================
# GraphSerializer — containers
# ==========================================================================

def test_dict_structure_and_keys_ordered():
    _, ids, by_id = _graph([{"a": 1, "b": [10, 20]}])
    root = by_id[ids[0]]
    assert _kind(root) == "dict"
    assert _length(root) == 2
    assert [k for k, _ in _items(root)] == ["a", "b"]
    b_id = dict(_items(root))["b"]
    assert _kind(by_id[b_id]) == "list"
    assert _length(by_id[b_id]) == 2


@pytest.mark.parametrize(
    "factory,kind",
    [
        (lambda: [1, 2, 3], "list"),
        (lambda: (1, 2, 3), "tuple"),
        (lambda: {1, 2, 3}, "set"),
        (lambda: frozenset([1, 2, 3]), "frozenset"),
    ],
)
def test_sequence_kinds(factory, kind):
    _, ids, by_id = _graph([factory()])
    node = by_id[ids[0]]
    assert _kind(node) == kind
    assert _length(node) == 3
    assert _type_name(node) is None       # plain builtin -> no type_name


@pytest.mark.parametrize(
    "factory,kind,n,cap,items_expected,truncated",
    [
        (lambda n: list(range(n)), "list", 5, 5, 5, False),
        (lambda n: list(range(n)), "list", 6, 5, 5, True),
        (lambda n: tuple(range(n)), "tuple", 5, 5, 5, False),
        (lambda n: tuple(range(n)), "tuple", 6, 5, 5, True),
        (lambda n: set(range(n)), "set", 5, 5, 5, False),
        (lambda n: set(range(n)), "set", 6, 5, 5, True),
        (lambda n: frozenset(range(n)), "frozenset", 5, 5, 5, False),
        (lambda n: frozenset(range(n)), "frozenset", 6, 5, 5, True),
        (lambda n: {i: i for i in range(n)}, "dict", 5, 5, 5, False),
        (lambda n: {i: i for i in range(n)}, "dict", 6, 5, 5, True),
    ],
)
def test_container_length_cap_boundary(factory, kind, n, cap, items_expected, truncated):
    _, ids, by_id = _graph([factory(n)], max_container=cap)
    node = by_id[ids[0]]
    assert _kind(node) == kind
    assert _length(node) == n                     # REAL length recorded
    assert _trunc(node) is truncated
    assert len(_items(node)) == items_expected     # only cap items serialized


def test_container_limit_large_records_real_length():
    _, ids, by_id = _graph([list(range(1000))], max_container=10)
    node = by_id[ids[0]]
    assert _length(node) == 1000
    assert _trunc(node) is True
    assert len(_items(node)) == 10


@pytest.mark.parametrize(
    "factory,expected_type_suffix",
    [
        (lambda: type("MD", (dict,), {})(), "MD"),
        (lambda: type("ML", (list,), {})([1, 2]), "ML"),
    ],
)
def test_container_subclass_records_type_name(factory, expected_type_suffix):
    obj = factory()
    _, ids, by_id = _graph([obj])
    node = by_id[ids[0]]
    assert _type_name(node).endswith("." + expected_type_suffix)


def test_dict_subclass_kind_is_dict_list_subclass_kind_is_list():
    md = type("MD2", (dict,), {})(a=1)
    ml = type("ML2", (list,), {})([9])
    _, ids, by_id = _graph([md, ml])
    assert _kind(by_id[ids[0]]) == "dict"
    assert _kind(by_id[ids[1]]) == "list"


def test_empty_containers():
    _, ids, by_id = _graph([[], {}, set(), (), frozenset()])
    for i in ids:
        assert _length(by_id[i]) == 0
        assert _trunc(by_id[i]) is False
        assert _items(by_id[i]) == []


# ==========================================================================
# GraphSerializer — non-string dict keys
# ==========================================================================

def test_non_string_keys_rendered_via_repr():
    _, ids, by_id = _graph([{5: "v", (1, 2): "w"}])
    keys = [k for k, _ in _items(by_id[ids[0]])]
    assert "5" in keys
    assert "(1, 2)" in keys


def test_long_string_key_truncated_to_repr_limit():
    long_key = "k" * 500
    _, ids, by_id = _graph([{long_key: 1}], repr_limit=20)
    key = _items(by_id[ids[0]])[0][0]
    assert len(key) == 21               # 20 chars + the ellipsis char
    assert key.endswith("…")


# ==========================================================================
# GraphSerializer — cycles & aliasing (identity)
# ==========================================================================

def test_self_referential_dict():
    d = {}
    d["self"] = d
    _, ids, by_id = _graph([d])
    assert dict(_items(by_id[ids[0]]))["self"] == ids[0]


def test_self_referential_list():
    lst = []
    lst.append(lst)
    _, ids, by_id = _graph([lst])
    assert _items(by_id[ids[0]])[0][1] == ids[0]


def test_mutual_cycle():
    a, b = {}, {}
    a["b"] = b
    b["a"] = a
    _, ids, by_id = _graph([a])
    a_node = by_id[ids[0]]
    b_id = dict(_items(a_node))["b"]
    assert dict(_items(by_id[b_id]))["a"] == ids[0]  # points back at a


@pytest.mark.parametrize(
    "shared_factory",
    [
        lambda: {"x": 1},
        lambda: [1, 2, 3],
        lambda: object(),
        lambda: (9, 9, 9),
    ],
)
def test_aliasing_same_object_gets_one_node(shared_factory):
    shared = shared_factory()
    container = {"a": shared, "b": [shared]}
    _, ids, by_id = _graph([container])
    root = by_id[ids[0]]
    a_id = dict(_items(root))["a"]
    b_list_id = dict(_items(root))["b"]
    inner = by_id[b_list_id]
    b_id = _items(inner)[0][1]
    assert a_id == b_id  # same identity -> same node


def test_aliasing_across_multiple_roots():
    shared = {"k": "v"}
    g = GraphSerializer()
    ia = g.add_root({"s": shared})
    ib = g.add_root([shared])
    nodes = g.run()
    by_id = {n[0]: n for n in nodes}
    from_a = dict(_items(by_id[ia]))["s"]
    from_b = _items(by_id[ib])[0][1]
    assert from_a == from_b


@pytest.mark.parametrize(
    "factory",
    [lambda: {"x": 1}, lambda: [1, 2], lambda: object()],
)
def test_distinct_equal_objects_get_distinct_ids(factory):
    a, b = factory(), factory()
    assert a is not b
    _, ids, _ = _graph([a, b])
    assert ids[0] != ids[1]


def test_every_referenced_id_resolves_to_a_node():
    obj = {"a": {"b": {"c": [1, 2, {"d": 3}]}}}
    _, _, by_id = _graph([obj])
    for node in by_id.values():
        for _k, vid in _items(node):
            assert vid in by_id


# ==========================================================================
# GraphSerializer — depth limit
# ==========================================================================

def _nest(depth):
    root = cur = {}
    for _ in range(depth):
        nxt = {}
        cur["n"] = nxt
        cur = nxt
    return root


@pytest.mark.parametrize("max_depth", [1, 2, 3, 4])
def test_depth_limit_produces_truncated_node(max_depth):
    _, _, by_id = _graph([_nest(20)], max_depth=max_depth)
    kinds = {_kind(n) for n in by_id.values()}
    assert "truncated" in kinds


def test_max_depth_zero_truncates_root_container():
    _, ids, by_id = _graph([{"a": 1}], max_depth=0)
    assert _kind(by_id[ids[0]]) == "truncated"


def test_max_depth_zero_still_renders_scalars():
    # scalars are handled before the depth check, so they survive.
    _, ids, by_id = _graph([42, "hi", None], max_depth=0)
    assert [_kind(by_id[i]) for i in ids] == ["int", "str", "none"]


def test_truncated_leaf_keeps_repr_and_type():
    _, _, by_id = _graph([_nest(5)], max_depth=1)
    trunc_nodes = [n for n in by_id.values() if _kind(n) == "truncated"]
    assert trunc_nodes
    n = trunc_nodes[0]
    assert n[3] is not None          # type_name kept
    assert n[5] is True              # truncated flag


# ==========================================================================
# GraphSerializer — byte / time budgets
# ==========================================================================

def test_byte_budget_marks_truncated_and_emits_placeholders():
    big = {"a": {"b": {"c": [1, 2, 3]}}}
    g = GraphSerializer(max_bytes=1)
    g.add_root(big)
    nodes = g.run()
    by_id = {n[0]: n for n in nodes}
    assert g.truncated is True
    for n in nodes:
        for _k, vid in _items(n):
            assert vid in by_id  # every referenced id still resolves


def test_deadline_budget_expires_immediately():
    g = GraphSerializer(deadline_ms=-1000)
    g.add_root({"a": {"b": 1}})
    nodes = g.run()
    assert g.truncated is True
    assert all(_kind(n) == "truncated" for n in nodes)


def test_generous_budget_not_truncated():
    g = GraphSerializer()  # default generous budgets
    g.add_root({"a": [1, 2, 3], "b": {"c": 4}})
    g.run()
    assert g.truncated is False


# ==========================================================================
# GraphSerializer — safe_repr / hostile objects
# ==========================================================================

def test_evil_repr_does_not_raise():
    class Evil:
        def __repr__(self):
            raise RuntimeError("boom")

    _, ids, by_id = _graph([Evil()])
    assert "repr failed" in _repr(by_id[ids[0]])


def test_evil_repr_base_exception():
    class Nasty:
        def __repr__(self):
            raise KeyboardInterrupt()

    _, ids, by_id = _graph([Nasty()])
    assert "repr failed" in _repr(by_id[ids[0]])


def test_repr_limit_truncation():
    class Big:
        def __repr__(self):
            return "z" * 1000

    _, ids, by_id = _graph([Big()], repr_limit=30)
    rep = _repr(by_id[ids[0]])
    assert len(rep) == 31            # 30 + ellipsis
    assert rep.endswith("…")


def test_iteration_failure_is_caught_as_describe_failed():
    class BadIter(list):
        def __iter__(self):
            raise RuntimeError("nope")

    _, ids, by_id = _graph([BadIter([1, 2])])
    node = by_id[ids[0]]
    assert _kind(node) == "object"
    assert "describe failed" in _repr(node)


# ==========================================================================
# GraphSerializer — opaque leaves
# ==========================================================================

def _a_function():
    return 1


class _AClass:
    def method(self):
        return 2


_OPAQUE_OBJECTS = [
    os,                       # module
    len,                      # builtin function
    _a_function,              # python function
    lambda: 0,                # lambda (FunctionType)
    _AClass,                  # class / type
    GraphSerializer,          # another class
    _a_function.__code__,     # code object
    _AClass().method,         # bound method
    [].append,                # builtin method
]


@pytest.mark.parametrize("obj", _OPAQUE_OBJECTS)
def test_opaque_types_are_leaves(obj):
    _, ids, by_id = _graph([obj])
    node = by_id[ids[0]]
    assert _kind(node) == "object"
    assert _items(node) == []       # never expanded
    assert _type_name(node) is not None


def test_frame_is_opaque_leaf():
    frame = sys._getframe()
    _, ids, by_id = _graph([frame])
    node = by_id[ids[0]]
    assert _kind(node) == "object"
    assert _items(node) == []


def test_traceback_is_opaque_leaf():
    try:
        raise ValueError("x")
    except ValueError:
        tb = sys.exc_info()[2]
    _, ids, by_id = _graph([tb])
    node = by_id[ids[0]]
    assert _kind(node) == "object"
    assert _items(node) == []


# ==========================================================================
# GraphSerializer — custom objects (__dict__ / __slots__)
# ==========================================================================

def test_object_with_dict_attrs_expanded():
    class C:
        def __init__(self):
            self.x = 1
            self.y = "hi"

    _, ids, by_id = _graph([C()])
    node = by_id[ids[0]]
    assert _kind(node) == "object"
    rendered = {k: _repr(by_id[v]) for k, v in _items(node)}
    assert rendered["x"] == "1"
    assert rendered["y"] == "hi"
    assert _length(node) == 2


def test_object_with_slots_expanded():
    class S:
        __slots__ = ("a", "b")

        def __init__(self):
            self.a = 10
            self.b = 20

    _, ids, by_id = _graph([S()])
    node = by_id[ids[0]]
    rendered = {k: _repr(by_id[v]) for k, v in _items(node)}
    assert rendered == {"a": "10", "b": "20"}


def test_object_with_string_slots():
    class S:
        __slots__ = "only"

        def __init__(self):
            self.only = 7

    _, ids, by_id = _graph([S()])
    rendered = {k: _repr(by_id[v]) for k, v in _items(by_id[ids[0]])}
    assert rendered == {"only": "7"}


def test_object_with_unset_slot_skipped():
    class S:
        __slots__ = ("a", "b")

        def __init__(self):
            self.a = 1  # b left unset

    _, ids, by_id = _graph([S()])
    keys = {k for k, _ in _items(by_id[ids[0]])}
    assert keys == {"a"}  # unset slot raises AttributeError -> skipped


def test_object_with_dict_and_slots_both():
    class Both:
        __slots__ = ("s", "__dict__")

        def __init__(self):
            self.s = 1
            self.d = 2

    _, ids, by_id = _graph([Both()])
    keys = {k for k, _ in _items(by_id[ids[0]])}
    # Both the __dict__ contents (d) and the explicit slots (s, __dict__) show up.
    assert {"s", "d"} <= keys


def test_plain_object_no_attrs():
    _, ids, by_id = _graph([object()])
    node = by_id[ids[0]]
    assert _kind(node) == "object"
    assert _items(node) == []
    assert _length(node) == 0


def test_object_attr_container_cap():
    class Many:
        def __init__(self):
            for i in range(50):
                setattr(self, f"a{i}", i)

    _, ids, by_id = _graph([Many()], max_container=10)
    node = by_id[ids[0]]
    assert _length(node) == 50
    assert _trunc(node) is True
    assert len(_items(node)) == 10


# ==========================================================================
# GraphSerializer — scrubbing integration
# ==========================================================================

def test_scrub_dict_keys():
    _, ids, by_id = _graph([{"user": "bob", "password": "hunter2", "api_key": "sk"}])
    rendered = {k: _repr(by_id[v]) for k, v in _items(by_id[ids[0]])}
    assert rendered["user"] == "bob"
    assert rendered["password"] == REDACTED
    assert rendered["api_key"] == REDACTED


def test_scrub_object_attributes():
    class Creds:
        def __init__(self):
            self.host = "db"
            self.secret_token = "abc"

    _, ids, by_id = _graph([Creds()])
    rendered = {k: _repr(by_id[v]) for k, v in _items(by_id[ids[0]])}
    assert rendered["host"] == "db"
    assert rendered["secret_token"] == REDACTED


def test_add_local_redacts_sensitive_name():
    g = GraphSerializer()
    nid = g.add_local("password", "s3cr3t")
    by_id = {n[0]: n for n in g.run()}
    assert _kind(by_id[nid]) == "redacted"
    assert _repr(by_id[nid]) == REDACTED


def test_add_local_keeps_benign_name():
    g = GraphSerializer()
    nid = g.add_local("username", "bob")
    by_id = {n[0]: n for n in g.run()}
    assert _repr(by_id[nid]) == "bob"


def test_redacted_value_never_touched():
    class Boom:
        def __repr__(self):
            raise RuntimeError("should never be called")

    # Even a hostile value under a sensitive key is never repr'd.
    _, ids, by_id = _graph([{"password": Boom()}])
    child_id = _items(by_id[ids[0]])[0][1]
    assert _kind(by_id[child_id]) == "redacted"


def test_redacted_nodes_get_fresh_distinct_ids():
    g = GraphSerializer()
    a = g._redacted()
    b = g._redacted()
    assert a != b


def test_custom_scrubber_used_by_serializer():
    scr = Scrubber(patterns=("classified",))
    g = GraphSerializer(scrubber=scr)
    g.add_root({"classified": "x", "password": "kept-because-not-in-custom"})
    by_id = {n[0]: n for n in g.run()}
    root = [n for n in by_id.values() if _kind(n) == "dict"][0]
    rendered = {k: _repr(by_id[v]) for k, v in _items(root)}
    assert rendered["classified"] == REDACTED
    assert rendered["password"] == "kept-because-not-in-custom"


# ==========================================================================
# describe_shallow
# ==========================================================================

@pytest.mark.parametrize(
    "value,kind,rep,type_name,length",
    [
        (None, "none", "None", None, None),
        (True, "bool", "True", None, None),
        (False, "bool", "False", None, None),
        (42, "int", "42", None, None),
        (3.5, "float", "3.5", None, None),
    ],
)
def test_describe_shallow_scalars(value, kind, rep, type_name, length):
    assert describe_shallow(value) == (kind, rep, type_name, length)


def test_describe_shallow_str_records_length_and_truncates():
    kind, rep, tn, length = describe_shallow("abcdef", max_str=3)
    assert kind == "str"
    assert rep == "abc"
    assert length == 6


def test_describe_shallow_bytes():
    kind, rep, tn, length = describe_shallow(b"abc")
    assert kind == "bytes"
    assert length == 3


@pytest.mark.parametrize(
    "value,kind,length",
    [
        ([1, 2, 3], "list", 3),
        ((1, 2), "tuple", 2),
        ({1, 2, 3}, "set", 3),
        (frozenset([1]), "frozenset", 1),
        ({"a": 1}, "dict", 1),
    ],
)
def test_describe_shallow_containers(value, kind, length):
    k, rep, tn, real_len = describe_shallow(value)
    assert k == kind
    assert real_len == length


def test_describe_shallow_object():
    class C:
        def __repr__(self):
            return "C-instance"

    kind, rep, tn, length = describe_shallow(C())
    assert kind == "object"
    assert rep == "C-instance"
    assert tn.endswith(".C")


def test_describe_shallow_never_raises_on_evil_repr():
    class Evil:
        def __repr__(self):
            raise RuntimeError("boom")

    kind, rep, tn, length = describe_shallow(Evil())
    assert kind == "object"
    assert "repr failed" in rep


def test_describe_shallow_huge_int():
    kind, rep, tn, length = describe_shallow(10 ** 5000)
    assert kind == "int"
    assert rep.startswith("<int ")


def test_describe_shallow_object_repr_limit():
    class Big:
        def __repr__(self):
            return "q" * 500

    kind, rep, tn, length = describe_shallow(Big(), repr_limit=10)
    assert len(rep) == 11
    assert rep.endswith("…")


# ==========================================================================
# adapters
# ==========================================================================

def test_adapter_registered_and_used():
    from flight._adapters import Adapted, _REGISTRY

    class Matrix:
        pass

    key = f"{Matrix.__module__}.{Matrix.__qualname__}"
    _REGISTRY[key] = lambda m: Adapted("matrix", "3x3", {"rows": 3, "cols": 3})
    try:
        _, ids, by_id = _graph([Matrix()])
        node = by_id[ids[0]]
        assert _kind(node) == "matrix"
        assert _repr(node) == "3x3"
        assert {k for k, _ in _items(node)} == {"rows", "cols"}
    finally:
        del _REGISTRY[key]


def test_adapter_fields_become_child_nodes():
    from flight._adapters import Adapted, _REGISTRY

    class Vec:
        pass

    key = f"{Vec.__module__}.{Vec.__qualname__}"
    _REGISTRY[key] = lambda v: Adapted("vec", "len=2", {"data": [1, 2]})
    try:
        _, ids, by_id = _graph([Vec()])
        node = by_id[ids[0]]
        data_id = dict(_items(node))["data"]
        child = by_id[data_id]
        assert _kind(child) == "list"
        assert _length(child) == 2
    finally:
        del _REGISTRY[key]


def test_adapter_public_decorator_api():
    from flight._adapters import _REGISTRY

    class Widget:
        pass

    key = f"{Widget.__module__}.{Widget.__qualname__}"

    @flight.adapter(key)
    def _w(w):
        return flight.Adapted("widget", "a widget", {"n": 1})

    try:
        _, ids, by_id = _graph([Widget()])
        assert _kind(by_id[ids[0]]) == "widget"
    finally:
        _REGISTRY.pop(key, None)


def test_adapter_raising_falls_back_to_generic():
    from flight._adapters import _REGISTRY

    class Fragile:
        def __init__(self):
            self.value = 99

    key = f"{Fragile.__module__}.{Fragile.__qualname__}"

    def _boom(_obj):
        raise RuntimeError("adapter failed")

    _REGISTRY[key] = _boom
    try:
        _, ids, by_id = _graph([Fragile()])
        node = by_id[ids[0]]
        assert _kind(node) == "object"      # fell through to generic
        rendered = {k: _repr(by_id[v]) for k, v in _items(node)}
        assert rendered["value"] == "99"
    finally:
        _REGISTRY.pop(key, None)


def test_adapter_resolve_returns_none_for_unregistered():
    from flight._adapters import resolve

    class Unregistered:
        pass

    assert resolve(Unregistered()) is None
    assert resolve(42) is None


def test_adapter_resolve_matches_by_qualname():
    from flight._adapters import Adapted, _REGISTRY, resolve

    class Foo:
        pass

    key = f"{Foo.__module__}.{Foo.__qualname__}"
    fn = lambda o: Adapted("foo", "s")
    _REGISTRY[key] = fn
    try:
        assert resolve(Foo()) is fn
    finally:
        _REGISTRY.pop(key, None)


def test_adapted_dataclass_defaults():
    from flight._adapters import Adapted

    a = Adapted("k", "summary")
    assert a.kind == "k"
    assert a.summary == "summary"
    assert a.fields == {}
    b = Adapted("k2", "s2", {"x": 1})
    assert b.fields == {"x": 1}


def test_builtin_adapters_registered():
    from flight._adapters import _REGISTRY

    for name in (
        "numpy.ndarray",
        "pandas.core.frame.DataFrame",
        "pandas.core.series.Series",
    ):
        assert name in _REGISTRY


def test_numpy_adapter_if_available():
    np = pytest.importorskip("numpy")
    arr = np.arange(6).reshape(2, 3)
    _, ids, by_id = _graph([arr])
    node = by_id[ids[0]]
    assert _kind(node) == "ndarray"
    labels = {k for k, _ in _items(node)}
    assert "shape" in labels and "dtype" in labels


def test_pandas_dataframe_adapter_if_available():
    pd = pytest.importorskip("pandas")
    df = pd.DataFrame({"a": [1, 2], "b": [3, 4]})
    _, ids, by_id = _graph([df])
    node = by_id[ids[0]]
    assert _kind(node) == "dataframe"
    labels = {k for k, _ in _items(node)}
    assert "shape" in labels


def test_pandas_series_adapter_if_available():
    pd = pytest.importorskip("pandas")
    s = pd.Series([1, 2, 3])
    _, ids, by_id = _graph([s])
    node = by_id[ids[0]]
    assert _kind(node) == "series"


# ==========================================================================
# module-level default constants sanity
# ==========================================================================

@pytest.mark.parametrize(
    "const,expected",
    [
        (MAX_STR, 10 * 1024),
        (MAX_CONTAINER, 200),
        (MAX_DEPTH, 6),
        (REPR_LIMIT, 200),
    ],
)
def test_serialize_default_constants(const, expected):
    assert const == expected
