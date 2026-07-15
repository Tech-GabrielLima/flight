#!/usr/bin/env python
"""Measured overhead of leaving Flight on, on a real web app under load.

Not a microbenchmark: a Flask app served by waitress, driven by concurrent
clients, measuring end-to-end request latency (p50/p90/p99) and throughput with
the recorder off, on (default call/return granularity), and on with per-line
recording. The server runs in a separate process so the load driver is never
itself recorded; Flight records only the app's own handler code (stdlib and
site-packages are excluded by default), which is exactly what you'd record in
production.

Reproduce:
    pip install pyflight flask waitress
    python benchmarks/web_overhead.py                 # default load
    python benchmarks/web_overhead.py --concurrency 16 --requests 8000 --n 80

The numbers in the README were produced by this script; re-run it to reproduce.
"""

from __future__ import annotations

import argparse
import http.client
import json
import statistics
import subprocess
import sys
import threading
import time


# ---- the app under test (its handler code is what Flight records) ----------
def build_orders(n):
    out = []
    for i in range(n):
        item = {
            "id": i,
            "sku": f"SKU-{i % 97:03d}",
            "qty": (i * 7) % 13 + 1,
            "price": round((i % 50) + 0.99, 2),
        }
        item["total"] = round(item["qty"] * item["price"], 2)
        item["tier"] = "gold" if item["total"] > 200 else "silver" if item["total"] > 50 else "bronze"
        out.append(item)
    return out


def summarize(orders):
    total = 0.0
    by_tier = {}
    for o in orders:
        total += o["total"]
        by_tier[o["tier"]] = by_tier.get(o["tier"], 0) + 1
    return {
        "revenue": round(total, 2),
        "by_tier": by_tier,
        "avg": round(total / max(len(orders), 1), 2),
    }


def make_app(default_n):
    from flask import Flask, jsonify, request

    app = Flask(__name__)

    @app.get("/health")
    def health():
        return {"ok": True}

    @app.get("/orders")
    def orders():
        n = int(request.args.get("n", default_n))
        data = build_orders(n)
        return jsonify({"summary": summarize(data), "count": len(data)})

    return app


def serve(mode, port, default_n, threads):
    import logging

    logging.getLogger("waitress").setLevel(logging.ERROR)  # silence "Task queue depth"
    if mode in ("on", "on-lines"):
        import flight

        flight.install(record_lines=(mode == "on-lines"))
    from waitress import serve as wserve

    wserve(make_app(default_n), host="127.0.0.1", port=port, threads=threads, _quiet=True)


# ---- load driver -----------------------------------------------------------
def _pct(xs, p):
    xs = sorted(xs)
    if not xs:
        return 0.0
    k = (len(xs) - 1) * p / 100.0
    f = int(k)
    c = min(f + 1, len(xs) - 1)
    return xs[f] + (xs[c] - xs[f]) * (k - f)


def wait_ready(port, timeout=20.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
            c.request("GET", "/health")
            c.getresponse().read()
            c.close()
            return True
        except OSError:
            time.sleep(0.05)
    return False


def run_load(port, path, concurrency, total):
    per = total // concurrency
    lat = []
    lock = threading.Lock()

    def worker():
        conn = http.client.HTTPConnection("127.0.0.1", port)
        mine = []
        for _ in range(per):
            t = time.perf_counter()
            conn.request("GET", path)
            r = conn.getresponse()
            r.read()
            mine.append((time.perf_counter() - t) * 1000.0)
        conn.close()
        with lock:
            lat.extend(mine)

    ts = [threading.Thread(target=worker) for _ in range(concurrency)]
    t0 = time.perf_counter()
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    wall = time.perf_counter() - t0
    return lat, wall, per * concurrency


def measure_mode(mode, args):
    proc = subprocess.Popen(
        [sys.executable, __file__, "--serve", "--mode", mode, "--port", str(args.port),
         "--n", str(args.n), "--threads", str(args.threads)]
    )
    try:
        if not wait_ready(args.port):
            raise RuntimeError(f"server ({mode}) did not become ready")
        path = f"/orders?n={args.n}"
        run_load(args.port, path, args.concurrency, args.warmup)  # warm up
        lat, wall, count = run_load(args.port, path, args.concurrency, args.requests)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    return {
        "mode": mode,
        "count": count,
        "rps": count / wall,
        "mean": statistics.fmean(lat),
        "p50": _pct(lat, 50),
        "p90": _pct(lat, 90),
        "p99": _pct(lat, 99),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--serve", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--mode", default="off")
    ap.add_argument("--port", type=int, default=8137)
    ap.add_argument("--n", type=int, default=60, help="records built per request (handler work)")
    ap.add_argument("--threads", type=int, default=8, help="waitress worker threads")
    ap.add_argument("--concurrency", type=int, default=8, help="concurrent load clients")
    ap.add_argument("--requests", type=int, default=6000, help="measured requests")
    ap.add_argument("--warmup", type=int, default=600)
    args = ap.parse_args()

    if args.serve:
        serve(args.mode, args.port, args.n, args.threads)
        return

    import platform

    print(f"# flight web overhead — {platform.python_version()} on {platform.platform()}")
    print(f"# app: Flask + waitress ({args.threads} threads) · handler builds {args.n} records/request")
    print(f"# load: {args.concurrency} concurrent clients · {args.requests} requests (after {args.warmup} warmup)\n")

    rows = [measure_mode(m, args) for m in ("off", "on", "on-lines")]
    base = rows[0]
    hdr = f"{'mode':<10} {'rps':>9} {'mean(ms)':>9} {'p50':>8} {'p90':>8} {'p99':>8} {'p99 Δ':>8}"
    print(hdr)
    print("-" * len(hdr))
    label = {"off": "off", "on": "on", "on-lines": "on +lines"}
    for r in rows:
        dp99 = (r["p99"] / base["p99"] - 1) * 100 if base["p99"] else 0
        print(f"{label[r['mode']]:<10} {r['rps']:>9.0f} {r['mean']:>9.3f} "
              f"{r['p50']:>8.3f} {r['p90']:>8.3f} {r['p99']:>8.3f} "
              f"{('%+.0f%%' % dp99) if r['mode'] != 'off' else '—':>8}")
    print()
    for r in rows[1:]:
        print(f"# {label[r['mode']]}: p50 {r['p50']/base['p50']:.2f}x, "
              f"p99 {r['p99']/base['p99']:.2f}x, throughput {r['rps']/base['rps']*100:.0f}% of baseline")
    print("\n# machine-readable:")
    print(json.dumps({"env": {"python": platform.python_version(), "platform": platform.platform()},
                      "params": vars(args), "results": rows}, indent=2))


if __name__ == "__main__":
    main()
