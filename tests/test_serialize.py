"""The object-graph serializer — cycles, aliasing, budgets, scrubbing, adapters."""

from __future__ import annotations

import flight
from flight._scrub import REDACTED, Scrubber
from flight._serialize import GraphSerializer


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


def _items(node):
    return node[6]


def test_scalars():
    _, ids, by_id = _graph([None, True, 42, 3.5, "hi", b"xy"])
    kinds = [_kind(by_id[i]) for i in ids]
    assert kinds == ["none", "bool", "int", "float", "str", "bytes"]
    assert _repr(by_id[ids[2]]) == "42"
    assert _repr(by_id[ids[4]]) == "hi"


def test_dict_and_list_structure():
    _, ids, by_id = _graph([{"a": 1, "b": [10, 20]}])
    root = by_id[ids[0]]
    assert _kind(root) == "dict"
    assert root[4] == 2  # real length
    keys = [k for k, _v in _items(root)]
    assert keys == ["a", "b"]
    # follow "b" -> list of two ints
    b_id = dict(_items(root))["b"]
    blist = by_id[b_id]
    assert _kind(blist) == "list"
    assert len(_items(blist)) == 2


def test_cycles_terminate():
    d: dict = {}
    d["self"] = d
    _, ids, by_id = _graph([d])
    root = by_id[ids[0]]
    # the "self" child points back at the same node id
    assert dict(_items(root))["self"] == ids[0]


def test_aliasing_same_object_same_id():
    shared = {"x": 1}
    a = {"shared": shared}
    b = [shared]
    _, ids, _ = _graph([a, b])
    g = GraphSerializer()
    ida = g.add_root(a)
    idb = g.add_root(b)
    nodes = g.run()
    by_id = {n[0]: n for n in nodes}
    shared_from_a = dict(_items(by_id[ida]))["shared"]
    shared_from_b = _items(by_id[idb])[0][1]
    assert shared_from_a == shared_from_b  # the SAME object -> one node


def test_container_limit_truncates_but_records_real_length():
    big = list(range(1000))
    _, ids, by_id = _graph([big], max_container=10)
    root = by_id[ids[0]]
    assert root[4] == 1000  # real length preserved
    assert root[5] is True  # truncated
    assert len(_items(root)) == 10


def test_string_truncation_records_real_length():
    _, ids, by_id = _graph(["x" * 100], max_str=10)
    node = by_id[ids[0]]
    assert node[4] == 100
    assert node[5] is True
    assert len(_repr(node)) == 10


def test_depth_limit():
    # nested dicts deeper than max_depth become truncated leaves
    deep = cur = {}
    for _ in range(20):
        nxt: dict = {}
        cur["n"] = nxt
        cur = nxt
    _, ids, by_id = _graph([deep], max_depth=3)
    # walk down; at some point we hit a truncated node
    kinds = {_kind(n) for n in by_id.values()}
    assert "truncated" in kinds


def test_scrubbing_redacts_sensitive_keys():
    _, ids, by_id = _graph([{"user": "bob", "password": "hunter2", "api_key": "sk-1"}])
    root = by_id[ids[0]]
    rendered = {k: _repr(by_id[v]) for k, v in _items(root)}
    assert rendered["user"] == "bob"
    assert rendered["password"] == REDACTED
    assert rendered["api_key"] == REDACTED


def test_scrubbing_on_object_attributes():
    class Creds:
        def __init__(self):
            self.host = "db"
            self.secret_token = "abc"

    _, ids, by_id = _graph([Creds()])
    root = by_id[ids[0]]
    rendered = {k: _repr(by_id[v]) for k, v in _items(root)}
    assert rendered["host"] == "db"
    assert rendered["secret_token"] == REDACTED


def test_safe_repr_never_raises_on_evil_repr():
    class Evil:
        def __repr__(self):
            raise RuntimeError("boom")

    _, ids, by_id = _graph([Evil()])
    node = by_id[ids[0]]
    assert "repr failed" in _repr(node)


def test_budget_expiry_marks_truncated_and_emits_placeholders():
    # A zero byte budget forces truncation after the first node; every
    # referenced id must still resolve to a node (placeholder).
    big = {"a": {"b": {"c": [1, 2, 3]}}}
    g = GraphSerializer(max_bytes=1)
    rid = g.add_root(big)
    nodes = g.run()
    by_id = {n[0]: n for n in nodes}
    assert g.truncated
    # every referenced value_id has a node
    for n in nodes:
        for _k, vid in n[6]:
            assert vid in by_id


def test_opaque_types_are_leaves():
    import os

    _, ids, by_id = _graph([os, len, GraphSerializer])
    for i in ids:
        node = by_id[i]
        assert _kind(node) == "object"
        assert _items(node) == []  # not expanded


def test_adapter_is_used_when_registered():
    class Matrix:
        pass

    from flight._adapters import Adapted, _REGISTRY

    key = f"{Matrix.__module__}.{Matrix.__qualname__}"
    _REGISTRY[key] = lambda m: Adapted("matrix", "3x3", {"rows": 3, "cols": 3})
    try:
        _, ids, by_id = _graph([Matrix()])
        node = by_id[ids[0]]
        assert _kind(node) == "matrix"
        assert _repr(node) == "3x3"
        labels = {k for k, _v in _items(node)}
        assert labels == {"rows", "cols"}
    finally:
        del _REGISTRY[key]


def test_scrubber_matching():
    s = Scrubber()
    assert s.should_redact("password")
    assert s.should_redact("auth_token")
    assert s.should_redact("API_KEY")
    assert not s.should_redact("username")
    assert not s.should_redact(42)
