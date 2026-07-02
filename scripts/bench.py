#!/usr/bin/env python
"""Steady-state overhead baseline for Flight (P2: honest, bounded overhead).

Runs a CPU-bound workload of *interesting* Python code twice — once without
Flight, once with it recording every LINE/CALL/RETURN — and reports the ratio.
The workload deliberately lives in this file (interesting code), so it exercises
the real hot path: a callback per source line feeding the ring buffer.

Phase-0 target: black-box recording should stay well under a 2x slowdown on
line-heavy code, and near-free on code dominated by C-level work. This is a
baseline to track against, not a promise about any particular program.

Usage:
    python scripts/bench.py [--iters N] [--repeat R]
"""

from __future__ import annotations

import argparse
import statistics
import time

import flight


def line_heavy(n: int) -> int:
    """A line-heavy, call-light loop — the worst case for a LINE hook."""
    total = 0
    acc = []
    for i in range(n):
        x = i * i
        y = x % 7
        if y == 0:
            total += x
        else:
            total -= y
        acc.append(total & 0xFFFF)
    return sum(acc)


def _leaf(i: int) -> int:
    return (i * i) % 7


def call_heavy(n: int) -> int:
    """A call-heavy loop — what the always-on call-level black box records."""
    total = 0
    for i in range(n):
        total += _leaf(i)
    return total


def _time(fn, *args, repeat: int) -> float:
    best = float("inf")
    for _ in range(repeat):
        t0 = time.perf_counter()
        fn(*args)
        best = min(best, time.perf_counter() - t0)
    return best


def _bench(name: str, fn, iters: int, repeat: int, *, record_lines: bool) -> None:
    fn(2000)  # warm up
    base = _time(fn, iters, repeat=repeat)

    flight.install(force_include=(__file__,), record_lines=record_lines)
    # Events for a *single* run (each run does identical work), for an honest
    # per-event cost — not the cumulative count across repeats.
    s0 = flight.stats()["total_events"]
    fn(iters)
    events = flight.stats()["total_events"] - s0
    rec = _time(fn, iters, repeat=repeat)
    flight.uninstall()

    ratio = rec / base if base > 0 else float("inf")
    ns = (rec - base) * 1e9 / events if events else float("nan")
    mode = "line-level" if record_lines else "call-level"
    print(f"  {name:<12} [{mode:^10}]  base {base*1e3:7.1f}ms  "
          f"flight {rec*1e3:8.1f}ms  {ratio:6.1f}x  "
          f"{events:>9,} ev/run  {ns:6.0f} ns/ev")


def main() -> int:
    ap = argparse.ArgumentParser(description="Flight overhead baseline")
    ap.add_argument("--iters", type=int, default=200_000)
    ap.add_argument("--repeat", type=int, default=5)
    args = ap.parse_args()

    import flight._core as _c

    if "release" not in (getattr(_c, "__file__", "") or "") and __debug__:
        pass  # (can't reliably detect; just remind in the docstring/README)
    print(f"iterations {args.iters:,} (best of {args.repeat})")
    print("NOTE: build the extension with `maturin develop --release` for real numbers.\n")
    # Default black box: call-level granularity, always-on-able.
    _bench("call_heavy", call_heavy, args.iters, args.repeat, record_lines=False)
    _bench("line_heavy", line_heavy, args.iters, args.repeat, record_lines=False)
    # Opt-in fine granularity: a LINE callback per source line (costly until
    # the callback moves to native code — a planned Phase-1 optimization).
    _bench("line_heavy", line_heavy, args.iters, args.repeat, record_lines=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
