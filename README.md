# flight — a flight recorder for Python

> **Languages:** **English** · [Português](README.pt-BR.md)
>
> **Docs:** [Vision & product](VISION.md) · [Technical guide](TECHNICAL.md) · [The `.flight` format](docs/FORMAT.md)

> When a Python program dies, you shouldn't get a traceback and *good luck* — you should get the
> complete **black box** of the flight: every step the code took in its last moments, navigable,
> shareable, and (eventually) replayable in time.
>
> flight is a **post-mortem recorder** built the way you can actually leave it on: a lock-free ring
> buffer and the `.flight` file writer live in **Rust** (via PyO3), fed by CPython's **`sys.monitoring`**
> (PEP 669, 3.12+) so the steady-state overhead stays low. When an unhandled exception escapes, the
> recording is flushed to a self-describing, truncation-tolerant `.flight` file you can inspect.

```console
$ python -m flight run examples/crash.py
Traceback (most recent call last):
  ...
ZeroDivisionError: division by zero
[flight] recorded flight-48103-1783001126460.flight

$ python -m flight inspect flight-48103-1783001126460.flight
flight file : flight-48103-1783001126460.flight
format      : v1  (complete, index)
written by  : flight 0.0.1 at 2026-07-02T11:05:26
blocks      : META, EVENT_RING
environment :
    python   3.13.12
    platform Linux-7.0.12-x86_64-with-glibc2.42
    argv     examples/crash.py
    cwd      /home/you/flight
events      : 16 across 4 code objects
last events (most recent first):
    PY_UNWIND  crash.py:0      # ZeroDivisionError unwinding back up the stack…
    RAISE      crash.py:0      # …frame by frame, from compute_average →
    PY_UNWIND  crash.py:0      #    summarize → main
    RAISE      crash.py:0
    PY_START   crash.py:4      # compute_average was entered right before
    PY_RETURN  crash.py:0
    ...
```

(That's the default call-level black box. Pass `record_lines=True` — or a future CLI flag — to also
capture a `LINE` event per source line and see the exact lines executed.)

---

## Why

The real debugging loop is: add prints → try to reproduce → fail to reproduce → add more prints → wait
for it to happen again. A traceback tells you **where** a program died, almost never **why**. flight
attacks that loop by recording what actually happened, so the bug report writes itself.

Three bets underpin the project (see [VISION.md](VISION.md)):

1. **`sys.monitoring` (PEP 669)** finally makes instrumenting CPython cheap enough to leave on, because
   a callback can return `DISABLE` and never be called at that location again.
2. A debugging tool is **50% engine, 50% reading experience** — so a first-class viewer is a planned
   phase, not an afterthought.
3. The **shareable `.flight` file** is the viral vector: "open this and you'll see everything."

## What it is (and isn't)

**It is** a scoped, post-mortem recorder with a first-class viewer, evolving toward time-travel
debugging. **It is not** an APM, a live debugger (that's `pdb`), or a profiler.

## Status — Phase 0 (foundation) ✅

This release is the *hello-world of the whole stack*, end to end and fully tested:

- **`flight-format`** — the versioned, append-only, truncation-tolerant `.flight` format: header,
  typed blocks (msgpack + zstd), optional footer index.
- **`flight-reader`** — a tolerant parser: uses the footer index when present, falls back to a linear
  scan otherwise; keeps unknown block types as raw bytes; degrades to `partial` instead of failing.
- **`flight-core`** — the hot path in Rust: a lock-free per-thread ring buffer, a global logical clock,
  the code map, and the `.flight` writer, exposed to Python as `flight._core`.
- **`flight` (Python)** — `install()`/`uninstall()` wiring `sys.monitoring`, an `excepthook` that
  auto-dumps on crash, `capture()` for handled errors, and a `python -m flight run|inspect` CLI.

What a Phase-0 `.flight` contains: the process **environment** (META) and the **event ring** — the last
thousands of PY_START / LINE / RETURN / RAISE events across all threads, merged by logical time. That
already answers *"what path did the code take in the instants before it died?"*.

**Next:** Phase 1 adds frames, locals and the serialized object graph; Phase 1.5 a Textual TUI viewer.

## Install & build

Requires Python **3.12+** and a Rust toolchain.

```console
python -m venv .venv && . .venv/bin/activate
pip install maturin pytest
maturin develop            # compiles the Rust core and installs `flight` into the venv
```

## Use

```python
import flight
flight.install()           # start recording (returns the active Config)

# ... run your program ...
# On an uncaught exception, a .flight file is written automatically.

flight.capture()           # or dump a .flight right now, e.g. inside an except block
flight.stats()             # {'total_events': ..., 'threads': ..., 'codes': ..., 'ring_capacity': ...}
flight.uninstall()         # restore the interpreter
```

Or wrap a script without editing it:

```console
python -m flight run myscript.py --its --args
python -m flight inspect crash.flight
```

Configuration (`flight.Config`): `ring_capacity`, `output_dir`, `dump_on_crash`, `record_lines`, and
the `deny_prefixes` / `force_include` policy that keeps stdlib and site-packages out of the recording
by default.

## Overhead — the honest picture

flight records only *your* code (stdlib and site-packages are excluded by default), and by default at
**call/return/exception** granularity — cheap, and enough to answer "which functions ran and how did
the exception unwind?". Per-line detail (`record_lines=True`) is opt-in.

Measured baseline (`python scripts/bench.py`, 200k iterations, this machine):

| Workload | Mode | Slowdown | Cost / event |
|---|---|---:|---:|
| line-heavy loop (no calls in the hot path) | call-level (default) | **~1.0x** | — |
| function called every iteration | call-level (default) | ~49x | ~500 ns |
| line-heavy loop | line-level (opt-in) | ~70x | ~350 ns |

The takeaway is the per-event cost: **~350–500 ns**, spent almost entirely in the Python-level
`sys.monitoring` callback and the FFI hop — *not* in the Rust ring buffer. So overhead is near-zero
when your recorded code isn't in the innermost hot loop, and grows linearly with how many events that
loop generates. Hitting the <5% black-box target on hot code needs the callback itself to run in
native code (a `PyCFunction` registered directly), which is a named Phase-1 optimization (see
[TECHNICAL.md](TECHNICAL.md) §0.2). Phase 0 is honest about being the *didactic* Python-callback
baseline.

## Design principles (the five inviolables)

- **P1 — Primum non nocere.** The recorder never crashes the user's program. Every callback swallows
  its own errors; every Rust FFI entry point is `catch_unwind`-guarded. A partial `.flight` is fine; a
  crash caused by flight is not.
- **P2 — Honest, bounded overhead.** Black-box mode targets <5% overhead.
- **P3 — The `.flight` format is the spine.** Engine and viewer only speak through it; it's versioned
  from day one; new readers read old files, old readers skip unknown blocks.
- **P4 — Every phase is useful on its own.**
- **P5 — Privacy by design.** Redaction of sensitive fields is a Phase-1 feature, not a later patch.

## Tests

```console
cargo test                 # Rust: format round-trips, truncation at every byte, ring, recorder
pytest                     # Python: monitoring wiring, crash capture, CLI, round-trip
python scripts/bench.py    # steady-state overhead baseline
```

## Layout

```
crates/
  flight-format/   the .flight format: blocks, events, writer
  flight-reader/   tolerant parser + query surface
  flight-core/     ring buffer, recorder, PyO3 bindings (module flight._core)
python/flight/     public API, sys.monitoring wiring, CLI
tests/             Python integration tests
scripts/bench.py   overhead baseline
```

## License

MIT.
