"""Phase 9 — cross-language recorders and the WASM viewer.

These prove the VISION's language-agnostic-format claim *concretely*: a `.flight`
written by Go or Node is read by the real Rust/Python reader, and the reader
compiled to WebAssembly parses a `.flight` in a JS runtime. Each test skips
cleanly when its toolchain (go / node / the built wasm) is absent, so the suite
stays green everywhere while giving full coverage where the tools exist.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

import flight

REPO = Path(__file__).resolve().parent.parent
GO_DIR = REPO / "recorders" / "go"
NODE_DIR = REPO / "recorders" / "node"
WASM = REPO / "crates" / "flight-wasm" / "target" / "wasm32-unknown-unknown" / "release" / "flight_wasm.wasm"


def _which(name: str, *extra: str):
    found = shutil.which(name)
    if found:
        return found
    for cand in extra:
        if Path(cand).exists():
            return cand
    return None


GO = _which("go", "/usr/local/go/bin/go")
NODE = _which("node", "/usr/bin/node", "/usr/local/bin/node")


# -- Go recorder ------------------------------------------------------------


@pytest.mark.skipif(GO is None, reason="Go toolchain not installed")
def test_go_recorder_writes_a_flight_the_reader_reads(tmp_path):
    out = tmp_path / "go.flight"
    env = {**os.environ, "GOCACHE": str(tmp_path / "gocache"), "GOFLAGS": "-mod=mod"}
    proc = subprocess.run(
        [GO, "run", "./cmd/demo", str(out)],
        cwd=str(GO_DIR),
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert out.exists()

    f = flight.read(out)
    assert not f.partial
    assert f.used_index
    assert f.event_count == 6
    assert f.code_count == 2
    kinds = [e[0] for e in f.events(10)]
    assert kinds[0] == "PY_START" and "RAISE" in kinds
    # code map resolved the events to Go file/function names
    qualnames = {e[2] for e in f.events(10)}
    assert "processRefund" in qualnames
    assert f.meta.get("platform")  # Go filled the environment


# -- Node recorder ----------------------------------------------------------


@pytest.mark.skipif(NODE is None, reason="Node.js not installed")
def test_node_recorder_writes_a_flight_the_reader_reads(tmp_path):
    out = tmp_path / "node.flight"
    proc = subprocess.run(
        [NODE, "demo.mjs", str(out)],
        cwd=str(NODE_DIR),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert out.exists()

    f = flight.read(out)
    assert not f.partial
    assert f.event_count == 6
    assert f.code_count == 2
    qualnames = {e[2] for e in f.events(10)}
    assert "handleRequest" in qualnames
    assert f.meta.get("python_version", "").startswith("node-")


# -- WASM viewer ------------------------------------------------------------

_NODE_WASM_LOADER = textwrap.dedent(
    """
    import { readFileSync } from "node:fs";
    const [wasmPath, flightPath] = process.argv.slice(2);
    const { instance } = await WebAssembly.instantiate(readFileSync(wasmPath), {});
    const ex = instance.exports;
    const data = readFileSync(flightPath);
    const ptr = ex.alloc(data.length);
    new Uint8Array(ex.memory.buffer).set(data, ptr);
    const resPtr = ex.parse(ptr, data.length);
    const dv = new DataView(ex.memory.buffer);
    const len = dv.getUint32(resPtr, true);
    const jsonBytes = new Uint8Array(ex.memory.buffer).slice(resPtr + 4, resPtr + 4 + len);
    ex.free(resPtr, 4 + len);
    ex.dealloc(ptr, data.length);
    process.stdout.write(Buffer.from(jsonBytes).toString("utf-8"));
    """
)


def _crash_py_flight(path: Path) -> Path:
    def parse_order(d):
        return d["items"][5]

    flight.install(output_dir=path.parent)
    try:
        parse_order({"items": [1, 2]})
    except IndexError:
        flight.capture(path=str(path))
    flight.uninstall()
    return path


@pytest.mark.skipif(NODE is None, reason="Node.js not installed")
@pytest.mark.skipif(
    not WASM.exists(), reason="wasm not built — run scripts/build-wasm.sh"
)
def test_wasm_reader_parses_a_real_flight_in_a_js_runtime(tmp_path):
    """The Rust reader compiled to WASM, decoding a real (C-zstd) `.flight` with
    the pure-Rust zstd decoder, driven from a JS runtime."""
    src = _crash_py_flight(tmp_path / "crash.flight")
    loader = tmp_path / "loader.mjs"
    loader.write_text(_NODE_WASM_LOADER)

    proc = subprocess.run(
        [NODE, str(loader), str(WASM), str(src)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    summary = json.loads(proc.stdout)
    assert summary["partial"] is False
    assert "EXCEPTION" in summary["blocks"]
    assert summary["exceptions"][0]["type"] == "IndexError"
    assert summary["frames"][0]["qualname"].endswith("parse_order")


def test_wasm_viewer_page_is_self_contained_if_built():
    """If the viewer page was generated, it inlines the wasm (works offline)."""
    page = REPO / "viewer-wasm" / "index.html"
    if not page.exists():
        pytest.skip("viewer page not built — run scripts/build-wasm.sh")
    html = page.read_text()
    assert "WASM_BASE64" in html
    assert "__WASM_BASE64__" not in html  # placeholder was substituted
    assert "WebAssembly.instantiate" in html
