from __future__ import annotations

import json
import sys
import threading
import urllib.request

import pytest

import flight
from flight._whatif import make_whatif_server, run_whatif

_PEP667 = sys.version_info >= (3, 13)


def pick(items):
    idx = 0
    return items[idx]


def _crash_pick(path):
    flight.install()
    try:
        pick([])
    except IndexError:
        flight.capture(path=str(path))
    finally:
        flight.uninstall()
    return str(path)


def _crash_line(path):
    return flight.read(path).crash().frames[0].lineno


def test_run_whatif_resolves_and_flips_outcome(tmp_path):
    p = _crash_pick(tmp_path / "c.flight")
    out = run_whatif(p, "items", [42], line=_crash_line(p))
    assert out["ok"]
    assert "IndexError" in out["baseline"]
    if _PEP667:
        assert out["applied"]
        assert out["changed"]
        assert "42" in out["counterfactual"]


def test_run_whatif_unresolvable(tmp_path):
    flight.install()
    try:
        p = str(tmp_path / "snap.flight")
        flight.capture(path=p)
    finally:
        flight.uninstall()
    out = run_whatif(p, "x", 1, line=1)
    assert not out["ok"]


@pytest.mark.skipif(not _PEP667, reason="live-local override needs Python 3.13+ (PEP 667)")
def test_http_whatif_roundtrip(tmp_path):
    p = _crash_pick(tmp_path / "c.flight")
    server = make_whatif_server(p, "127.0.0.1", 0)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        url = f"http://127.0.0.1:{port}"
        with urllib.request.urlopen(url + "/") as r:
            page = r.read().decode()
        # the rich browser viewer, with the recording embedded and the engine live
        assert "window.__FLIGHT__" in page and "engine:true" in page.replace(" ", "")
        assert "black box viewer" in page.lower()
        body = json.dumps({"var": "items", "value": [7, 8], "line": _crash_line(p)}).encode()
        req = urllib.request.Request(
            url + "/whatif", data=body, headers={"Content-Type": "application/json"}, method="POST"
        )
        with urllib.request.urlopen(req) as r:
            d = json.loads(r.read())
        assert d["ok"] and d["changed"]
        assert "IndexError" in d["baseline"]
        assert "7" in d["counterfactual"]
        # /fix proposes and verifies a patch over the recorded tape
        req = urllib.request.Request(
            url + "/fix", data=b"{}", headers={"Content-Type": "application/json"}, method="POST"
        )
        with urllib.request.urlopen(req) as r:
            fx = json.loads(r.read())
        assert fx["ok"] and isinstance(fx["status"], str) and "report" in fx
    finally:
        server.shutdown()
        server.server_close()
