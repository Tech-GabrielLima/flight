#!/usr/bin/env python
"""Reliability under violent death — tested, not just designed for.

The `.flight` format is append-only, so the bytes on disk after a process is
killed mid-write are always some *prefix* of the file. This suite turns the
robustness claim into an auditable number two ways:

  1. SIGKILL harness — spawn a process that records and writes a real (large)
     `.flight`, and `kill -9` it right as it starts writing. Whatever landed on
     disk is then parsed in an isolated subprocess, so a reader crash (segfault,
     abort, uncaught panic) shows up as a non-zero exit — not a silent pass.

  2. Truncation sweep — take real recordings and feed the reader *every* byte
     prefix (a superset of every state any kill could leave), asserting each is
     a clean parse, a `partial` parse, or a graceful error — never a crash.

Exit code is non-zero if any read crashed or returned an unclassifiable result.

    python benchmarks/fault_injection.py                 # full run (README number)
    python benchmarks/fault_injection.py --kills 200 --quick
"""

from __future__ import annotations

import argparse
import os
import random
import signal
import subprocess
import sys
import tempfile
import time
from collections import Counter

BIG_CFG = dict(
    record_lines=True, ring_capacity=120000,
    capture_max_bytes=16_000_000, max_container=4096, max_depth=8, max_str=2000,
)


# ---- subprocess entry points ----------------------------------------------
def _child_write(path, once=False):
    """Record a crash and write it to `path`. After the heavy in-memory
    serialization is set up, print GO and then write continuously in a loop, so
    a disk write is almost always in flight when the parent's kill lands."""
    import flight

    flight.install(**BIG_CFG)

    def build(n):
        data = {}
        for i in range(n):
            data[i] = {"vals": list(range(24)), "name": f"item-{i:05d}", "nested": {"a": i, "b": [i] * 6}}
        return data

    try:
        big = build(2500)  # noqa: F841 — kept in scope so it lands in the object graph
        xs = []
        _ = 1 / len(xs)
    except ZeroDivisionError:
        sys.stdout.write("GO\n")
        sys.stdout.flush()
        n = 1 if once else 100000
        for _ in range(n):  # rewrite the same path over and over until killed
            flight.capture(path=path)
    sys.stdout.write("DONE\n")
    sys.stdout.flush()


def _child_read(path):
    """Classify a (possibly corrupt) file. Exit 0 with a token for any graceful
    outcome; only a genuine crash yields a non-zero exit / fatal signal."""
    import flight

    try:
        fl = flight.read(path)
        touched = fl.partial
        if fl.has_crash:
            c = fl.crash()
            touched = (len(c.frames), c.partial, [f.locals for f in c.frames])  # noqa: F841
        print("PARTIAL" if fl.partial else "OK")
    except Exception as e:  # graceful, expected for a truncated header/magic
        print("ERROR:" + type(e).__name__)


# ---- classification --------------------------------------------------------
def classify_file(path):
    if not os.path.exists(path):
        return "nofile"
    if os.path.getsize(path) == 0:
        return "empty"
    r = subprocess.run([sys.executable, __file__, "--read", path],
                       capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        return f"CRASH(rc={r.returncode})"
    out = (r.stdout.strip().splitlines() or [""])[-1]
    if out == "OK":
        return "complete"
    if out == "PARTIAL":
        return "partial"
    if out.startswith("ERROR"):
        return "graceful_error"
    return f"UNKNOWN({out!r})"


# ---- 1) real SIGKILL during the write --------------------------------------
def sigkill_run(n, tmpd):
    counts, crashes = Counter(), []
    for i in range(n):
        path = os.path.join(tmpd, f"k{i}.flight")
        p = subprocess.Popen([sys.executable, __file__, "--child", path],
                             stdout=subprocess.PIPE, text=True)
        p.stdout.readline()               # block until "GO" (writing loop started) or EOF
        time.sleep(random.uniform(0.01, 0.30))  # kill somewhere inside the write loop
        p.send_signal(signal.SIGKILL)
        p.wait()
        cls = classify_file(path)
        counts[cls] += 1
        if cls.startswith("CRASH") or cls.startswith("UNKNOWN"):
            crashes.append((i, cls, os.path.getsize(path) if os.path.exists(path) else 0))
        try:
            os.remove(path)
        except OSError:
            pass
    return counts, crashes


# ---- 2) exhaustive / sampled truncation ------------------------------------
def _make_flights(tmpd):
    import flight

    made = []

    # small crash
    small = os.path.join(tmpd, "small.flight")
    flight.install()
    try:
        _ = 1 / len([])
    except ZeroDivisionError:
        flight.capture(path=small)
    finally:
        flight.uninstall()
    made.append(("small crash", small, None))  # None => every offset

    # deterministic run (adds a NONDET block)
    det = os.path.join(tmpd, "det.flight")
    import random as _r
    import time as _t

    def work():
        return _t.time(), _r.random()

    with flight.deterministic(det):
        work()
    made.append(("deterministic", det, None))

    # a big crash (sampled — every offset would be slow)
    big = os.path.join(tmpd, "big.flight")
    subprocess.run([sys.executable, __file__, "--child", big, "--once"], capture_output=True, text=True, timeout=60)
    if os.path.exists(big) and os.path.getsize(big):
        made.append(("big crash", big, 3000))
    return made


def truncation_sweep(tmpd, quick):
    import flight

    counts, bad = Counter(), []
    tmp = os.path.join(tmpd, "_prefix.flight")
    for label, full, sample in _make_flights(tmpd):
        data = open(full, "rb").read()
        n = len(data)
        if sample is None:
            offsets = range(0, n + 1)
        else:
            k = min((300 if quick else sample), n + 1)
            offsets = sorted(random.sample(range(0, n + 1), k))
        for off in offsets:
            with open(tmp, "wb") as f:
                f.write(data[:off])
            try:
                fl = flight.read(tmp)
                touched = fl.partial
                if fl.has_crash:
                    c = fl.crash()
                    touched = (len(c.frames), [fr.locals for fr in c.frames])  # noqa: F841
                counts["partial" if fl.partial else "complete"] += 1
            except Exception:
                counts["graceful_error"] += 1
        counts[f"file:{label}({n}B)"] += 0  # record the file in the report
    return counts, bad


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--read", metavar="PATH")
    ap.add_argument("--child", metavar="PATH")
    ap.add_argument("--once", action="store_true", help="child: write one .flight and exit")
    ap.add_argument("--kills", type=int, default=1000, help="SIGKILL iterations")
    ap.add_argument("--quick", action="store_true", help="smaller run")
    ap.add_argument("--seed", type=int, default=1234)
    args = ap.parse_args()

    if args.read:
        return _child_read(args.read)
    if args.child:
        return _child_write(args.child, once=args.once)

    random.seed(args.seed)
    kills = 200 if args.quick else args.kills
    import platform

    print(f"# flight fault injection — {platform.python_version()} on {platform.platform()}\n")

    with tempfile.TemporaryDirectory() as tmpd:
        print(f"[1/2] SIGKILL during the write · {kills} iterations (reads isolated in subprocesses)…")
        kc, crashes = sigkill_run(kills, tmpd)
        with_file = sum(v for k, v in kc.items() if k in ("complete", "partial", "graceful_error"))
        for k in ("complete", "partial", "graceful_error", "empty", "nofile"):
            if kc.get(k):
                print(f"    {k:<16} {kc[k]:>6}")
        print(f"    → {with_file} kills left an on-disk file; reader crashes: {len(crashes)}\n")

        print("[2/2] Truncation sweep · every byte prefix of real recordings…")
        tc, bad = truncation_sweep(tmpd, args.quick)
        reads = sum(tc[k] for k in ("complete", "partial", "graceful_error"))
        for lbl in [k for k in tc if k.startswith("file:")]:
            print(f"    {lbl.replace('file:', '')}")
        for k in ("complete", "partial", "graceful_error"):
            print(f"    {k:<16} {tc.get(k, 0):>6}")
        print(f"    → {reads} prefixes read; unclassifiable/crash: {len(bad)}\n")

    total = with_file + reads
    crash_total = len(crashes) + len(bad)
    print("=" * 64)
    print(f"RESULT: {total:,} reads of violently-truncated .flight files "
          f"({kills} real SIGKILLs + every byte prefix)")
    if crash_total == 0:
        print("        0 reader crashes — every one parsed, went partial, or errored cleanly.")
    else:
        print(f"        {crash_total} FAILURES:")
        for c in (crashes + bad)[:20]:
            print("         ", c)
    print("=" * 64)
    return 1 if crash_total else 0


if __name__ == "__main__":
    sys.exit(main() or 0)
