from __future__ import annotations

import threading
import urllib.request

import flight
from flight._fleet import (
    FleetIndex,
    extract_record,
    make_server,
    report_to,
    safe_to_send,
)


def crash_zero():
    return 1 / 0


def crash_key():
    return {"a": 1}["missing"]


def _record(path, fn, *, commit=None, correlation=None):
    flight.install(commit=commit)
    if correlation is not None:
        flight.correlate(traceparent=correlation)
    try:
        fn()
    except Exception:
        flight.capture(path=str(path))
    finally:
        flight.uninstall()
    return str(path)


def test_extract_record(tmp_path):
    p = _record(tmp_path / "c.flight", crash_zero, commit="abc123")
    rec = extract_record(p)
    assert rec is not None
    assert rec.exc_type == "ZeroDivisionError"
    assert rec.commit == "abc123"
    assert rec.fingerprint


def test_ingest_and_top(tmp_path):
    idx = FleetIndex(tmp_path / "store")
    for i in range(3):
        idx.ingest_path(_record(tmp_path / f"z{i}.flight", crash_zero))
    idx.ingest_path(_record(tmp_path / "k.flight", crash_key))
    groups = idx.top_fingerprints()
    assert len(groups) == 2
    assert groups[0].count == 3
    assert groups[0].exc_type == "ZeroDivisionError"
    assert idx.stats() == {"total": 4, "distinct": 2}


def test_ingest_ignores_non_crash(tmp_path):
    idx = FleetIndex(tmp_path / "store")
    flight.install()
    try:
        p = str(tmp_path / "snap.flight")
        flight.capture(path=p)
    finally:
        flight.uninstall()
    assert idx.ingest_path(p) is None
    assert idx.stats()["total"] == 0


def test_occurrences(tmp_path):
    idx = FleetIndex(tmp_path / "store")
    p = _record(tmp_path / "z.flight", crash_zero, commit="c1")
    fp = extract_record(p).fingerprint
    idx.ingest_path(p)
    occ = idx.occurrences(fp[:6])
    assert len(occ) == 1 and occ[0]["commit_sha"] == "c1"


def test_regression_marks_new_since_deploy(tmp_path):
    idx = FleetIndex(tmp_path / "store")
    idx.add_deploy("deadbeef", when_ms=1000, label="deploy-42")
    p = _record(tmp_path / "z.flight", crash_zero)
    idx.ingest_path(p)
    with idx._conn() as c:
        c.execute("UPDATE crashes SET created_ms = 5000")
    groups = idx.regressions()
    assert groups[0].is_new
    assert groups[0].since_deploy == "deploy-42"


def test_no_deploys_no_regression_flag(tmp_path):
    idx = FleetIndex(tmp_path / "store")
    idx.ingest_path(_record(tmp_path / "z.flight", crash_zero))
    groups = idx.regressions()
    assert not groups[0].is_new
    assert groups[0].since_deploy is None


def test_trace_graph_groups_by_trace_id(tmp_path):
    idx = FleetIndex(tmp_path / "store")
    tp = "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"
    idx.ingest_path(_record(tmp_path / "a.flight", crash_zero, correlation=tp))
    idx.ingest_path(_record(tmp_path / "b.flight", crash_key, correlation=tp))
    graph = idx.trace_graph()
    assert len(graph) == 1
    (nodes,) = graph.values()
    assert len(nodes) == 2


def test_safe_to_send_flags_email(tmp_path):
    def crash_with_pii():
        user_email = "alice@example.com"
        return user_email[999]

    flight.install()
    try:
        crash_with_pii()
    except IndexError:
        flight.capture(path=str(tmp_path / "pii.flight"))
    finally:
        flight.uninstall()
    ok, concerns = safe_to_send(str(tmp_path / "pii.flight"))
    assert not ok
    assert any("email" in c for c in concerns)


def test_safe_to_send_clean(tmp_path):
    p = _record(tmp_path / "z.flight", crash_zero)
    ok, concerns = safe_to_send(p)
    assert ok and not concerns


def test_http_ingest_and_query(tmp_path):
    idx = FleetIndex(tmp_path / "store")
    server = make_server(idx, "127.0.0.1", 0)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        url = f"http://127.0.0.1:{port}"
        p = _record(tmp_path / "z.flight", crash_zero, commit="abc")
        assert report_to(url, p) is True
        with urllib.request.urlopen(url + "/") as r:
            body = r.read().decode()
        assert "flight fleet" in body
        import json

        with urllib.request.urlopen(url + "/api/top") as r:
            top = json.loads(r.read())
        assert top and top[0]["exc_type"] == "ZeroDivisionError" and top[0]["count"] == 1
    finally:
        server.shutdown()
        server.server_close()


def test_report_to_strict_refuses_unsafe(tmp_path):
    def crash_with_secret():
        token = "AKIAIOSFODNN7EXAMPLE"
        return token[999]

    flight.install()
    try:
        crash_with_secret()
    except IndexError:
        flight.capture(path=str(tmp_path / "s.flight"))
    finally:
        flight.uninstall()
    assert report_to("http://127.0.0.1:1", str(tmp_path / "s.flight"), strict=True) is False
