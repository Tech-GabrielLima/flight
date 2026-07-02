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
$ python examples/crash.py
Traceback (most recent call last):
  ...
ZeroDivisionError: division by zero
[flight] recorded flight-57275-1783002970970.flight

$ python -m flight inspect flight-57275-1783002970970.flight
flight file : flight-57275-1783002970970.flight
format      : v1  (complete, index)
blocks      : META, EXCEPTION, FRAME, OBJECT, SOURCE, EVENT_RING
exception   :
    ZeroDivisionError: division by zero
frames      : 4 (crash first)
  #0 compute_average  (crash.py:26)
        numbers = list[0] ↔          # empty! this is the bug
        total = 0
  #1 summarize  (crash.py:32)
        datasets = dict[3] ↔
        results = list[2]
        name = evening               # …and this names the culprit dataset
        data = list[0] ↔             # the SAME empty list, aliased into compute_average
        avg = 10.0
  #2 main  (crash.py:43)
        datasets = dict[3] ↔
  #3 <module>  (crash.py:47)
        ...
```

The `↔` marks an object that is the *same* across frames: `data` (the empty `evening` dataset) is
literally `numbers` inside `compute_average`. The black box diagnosed the bug — no reproduction
needed. Every frame's locals, the full object graph, the exception chain and the source are in the
file; `flight inspect --max-locals 40` shows more, and the Phase-1.5 TUI viewer will make it
navigable.

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

## Status — Phase 3 (re-execution) ✅

Every phase is complete, end to end and fully tested: 0 (foundation), 1 (the full black box), 1.5 (the
TUI viewer), 2 (scoped time-travel) and 3 (rungs 1–2 of re-execution).

**The engine (Rust):**
- **`flight-format`** — the versioned, append-only, truncation-tolerant `.flight` format: header,
  typed blocks (msgpack + zstd), optional footer index.
- **`flight-reader`** — a tolerant parser: footer-index fast path with a linear-scan fallback; keeps
  unknown block types as raw bytes; degrades to `partial` instead of failing. Query surface for the
  exception chain, frames, object graph and **aliasing**.
- **`flight-core`** — the hot path: a lock-free per-thread ring buffer, a global logical clock, the
  code map, and the `.flight` writer, exposed to Python as `flight._core`.

**The recorder (Python):** `install()`/`uninstall()` wiring `sys.monitoring`, an `excepthook` that
auto-writes the crash black box, `capture()` for handled errors, and a `python -m flight run|inspect`
CLI.

**What a `.flight` contains after Phase 1:**
- the process **environment** (META) and the **event ring** — the last thousands of
  PY_START / LINE / RETURN / RAISE events, merged by logical time (Phase 0);
- the **exception chain** (`__cause__` / `__context__`);
- every **stack frame**, crash-first, with its **locals**;
- the serialized **object graph** — identity-preserving, so aliasing (the *same* object in two
  frames) is visible; cycle-safe; with per-container/-string limits, a depth cap, and a global
  time + byte budget so a giant or hostile object can never blow up or hang the capture;
- the **source** of every file involved, so the values make sense on another machine.

Two safety properties are first-class: **scrubbing** (P5) redacts values whose name looks sensitive
(`password`, `token`, `secret`, …) before any byte is written, and every step is guarded so the
recorder can never crash the program it is recording (P1). Type **adapters** describe big objects
(numpy arrays, pandas frames) by shape/dtype/preview instead of dumping their contents.

**Phase 2 — time-travel of scope.** Inside a `with flight.record():` block, every state write is
recorded as a MUTATION, so afterwards you can replay the program's memory:

```python
import flight

with flight.record() as rec:
    cache = {}
    rec.watch(cache, name="cache")   # also track writes into this container
    running = 0
    for it in [5, 3, 8]:
        running = running + it       # a local rebind
        cache[it] = running          # a container write
```

```console
$ python -m flight timeline --var running flight-scope-*.flight
history of local 'running' (4 writes):
  #3     tt.py:8     local  running = 0
  #5     tt.py:10    local  running = 5
  #8     tt.py:10    local  running = 8
  #11    tt.py:10    local  running = 16       # ← how it evolved, step by step

$ python -m flight timeline --who cache flight-scope-*.flight
writes to 'cache' (3 writes):
  #6     tt.py:8     item   cache[5] = 5
  #9     tt.py:8     item   cache[3] = 8
  #12    tt.py:8     item   cache[8] = 16      # ← who wrote what, and when
```

From Python: `flight.read(path).recording()` gives a `Recording` with `history(name)`,
`who_mutated(name)`, and `state_at(seq)` (reconstruct the locals at any step — event sourcing).

**How writes are captured (honest engineering).** No bytecode surgery: inside the scope, each `LINE`
event diffs the frame's locals (→ local rebinds) and diffs each `watch()`-ed object's snapshot (→
container/attribute writes, without ever subclassing, so `type(x) is dict` still holds). A `LINE` event
fires *before* its line runs, so a detected change is attributed to the previous line executed — the
line that actually made the write — giving **exact line attribution**; and a frame's final write (no
trailing `LINE` event) is recovered at `PY_RETURN`/`PY_UNWIND`, so nothing is dropped. It is
line-granular (multiple writes on one physical line share that line) and robust across CPython versions;
per-instruction capture via native bytecode instrumentation is a documented future step
([TECHNICAL.md](TECHNICAL.md) §3.2). Recording is opt-in and scope-delimited, so its cost is only paid
around the code you're investigating (P2).

**Phase 1.5 — the viewer.** A [Textual](https://textual.textualize.io) TUI over the reader's query
surface (never bytes, P3):

```console
$ pip install 'flight-recorder[viewer]'
$ python -m flight view flight-*.flight
```

Left: a `Tree` of **frames → locals → object graph** with lazy expansion (a 100 MB `.flight` opens
instantly); objects that appear in more than one frame are marked `↔`. Right, in tabs: the **source**
of the selected frame with the crash line marked and **values shown inline** on the code, an object
**Detail** panel (type / value / aliasing), the **Exception** chain, the **Events** ring (what path the
code took), and — for a scope recording — the **Timeline** of mutations.

```text
 compute_average
 examples/crash.py:26

     22 def compute_average(numbers):
            ‹ numbers = list[0] ›               ← empty! the bug, inline on the code
     23     total = 0
     25         total += n
 ▶   26     return total / len(numbers)
            ‹ total = 0   numbers = list[0] ›
```

The rendering-free logic (inline values, alias index, source window) lives in `_viewer_model` and is
unit-tested without a terminal; the app is a thin shell, tested headlessly via Textual's `Pilot`.

**Phase 3 — re-execution.** Two rungs, plus their convergence.

*Rung 1 — a bug report that writes and checks itself.* From a crash `.flight`, `flight repro` rebuilds
the crash function's arguments from the object graph (aliasing and cycles preserved; opaque objects
become attribute stubs), embeds the source, calls it, and — running it in a subprocess — only labels it
*verified* if it actually reproduces:

```console
$ python -m flight repro crash.flight -o repro_bug.py
wrote repro_bug.py
  ✓ verified: it reproduces the same exception
```

*Rung 2 — deterministic replay.* A program is a deterministic function of its non-deterministic inputs.
Record only those and the run repeats bit-for-bit:

```python
import flight, time, random

def work():
    return time.time(), random.random()

with flight.deterministic("run.flight"):
    original = work()

assert flight.replay("run.flight", work) == original   # identical, though time/random moved on
```

`flight.replay` feeds the recorded values back in order; if the code calls a boundary in a different
order than recorded — control flow diverged — it raises `ReplayDivergence` pointing at the exact step.
Interposed boundaries: `time.*`, `random.*`, `uuid4`, `os.urandom`/`getpid`/`getenv`, `secrets.*`.

*The convergence.* A crash inside `deterministic()` writes the crash frames **and** the recorded
randomness into one file, so `flight repro` weaves the tape into the generated script — reproducing a
**flaky, timing/random-dependent crash deterministically, every run**.

Honest scope (rung 3): replay is guaranteed for single-thread / single-asyncio-loop code; files,
sockets and subprocess are recognized but staged (their state is larger). The clock / randomness / uuid
class — flaky tests, time bombs, "fails 1% of the time" — is covered.

## Install & build

Requires Python **3.12+** and a Rust toolchain.

```console
python -m venv .venv && . .venv/bin/activate
pip install maturin pytest textual   # textual is only needed for the TUI viewer
maturin develop --release            # compiles the Rust core (release) and installs it into the venv
```

(Plain `maturin develop` builds Rust in debug mode — fine for iterating, ~10× slower on the hot path.
Use `--release`, and release wheels for distribution.)

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
python -m flight view crash.flight        # interactive TUI (needs the [viewer] extra)
python -m flight timeline scope.flight    # a scope recording's mutation timeline
python -m flight repro crash.flight       # generate + verify a reproduction script
```

Configuration (`flight.Config`): `ring_capacity`, `output_dir`, `dump_on_crash`, `record_lines`, the
`deny_prefixes` / `force_include` policy that keeps stdlib and site-packages out of the recording, and
the crash-capture budget (`capture_deadline_ms`, `capture_max_bytes`, `max_str`, `max_container`,
`max_depth`, `repr_limit`, `scrub_patterns`).

Register an adapter for your own big types so they're summarized, not dumped:

```python
@flight.adapter("mypkg.Matrix")
def _(m):
    return flight.Adapted("matrix", f"{m.rows}x{m.cols}", {"rows": m.rows, "cols": m.cols})
```

## Overhead — the honest picture

flight records only *your* code (stdlib and site-packages are excluded by default, filtered in Rust),
and by default at **call/return/exception** granularity — enough to answer "which functions ran and how
did the exception unwind?". Per-line detail (`record_lines=True`) is opt-in.

The recording callbacks are **native Rust functions registered directly with `sys.monitoring`** — the
interpreter calls straight into Rust, with no Python callback frame and no second FFI hop. On the hot
path there is **no lock**: a **lock-free per-thread ring buffer** (a `fetch_add` and a 24-byte store)
and a **thread-local direct-mapped cache** of the "is this code interesting?" decision, so a hot loop
never touches a mutex.

Measured (`maturin develop --release`, then `python scripts/bench.py`, honest per-run cost):

| What | Cost / event |
|---|---:|
| `sys.monitoring` dispatch to a do-nothing callback (the floor) | **~40 ns** |
| dispatch + lock-free ring push (no filter) | ~55 ns |
| **full recorded event** (filter + register + push) | **~65 ns** |
| — for comparison, the old Python-callback + FFI path (debug) | ~350–500 ns |

So a recorded event costs **~65 ns**, ~2.5x slowdown on pathological code that calls a recorded
function every iteration, and **~1.0x** when your recorded code isn't the innermost hot loop (the
common case). That is roughly **7–8× faster** than the previous Python-callback path, and within ~1.5×
of the hard floor.

**Why not single-digit nanoseconds?** Because ~40 ns of *every* event is `sys.monitoring` itself —
measured, the cost of the interpreter dispatching to a callback that does **nothing**. No PEP 669 tool
can go below it. And the only sub-callback mechanism — injecting instrumentation into the bytecode —
still emits a Python-level `CALL` per event (tens of ns) and trades this floor for per-CPython-release
fragility. Single-digit ns per *recorded event* is not reachable in CPython by any per-event mechanism;
the cheapest per-event action the interpreter offers is already ~30–50 ns. flight sits just above that
floor: ~40 ns you cannot remove, ~25 ns of actual work (lock-free ring + lock-free filter cache).

> **Build note:** `maturin develop` compiles Rust in **debug** mode (~10× slower — fine for iterating).
> Use `maturin develop --release` (and release wheels) for the numbers above.

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
  flight-format/   the .flight format: blocks, events, crash payloads, writer
  flight-reader/   tolerant parser + query surface (exceptions, frames, object graph, aliases)
  flight-core/     ring buffer, recorder, PyO3 bindings (module flight._core)
python/flight/
  _install.py      sys.monitoring wiring + exception hooks + scope stack
  _capture.py      the crash-capture algorithm (frames, locals, sources, chain)
  _record.py       the `with flight.record()` scope + watch() (Phase 2)
  _serialize.py    the object-graph serializer (identity, budget, limits)
  _scrub.py        sensitive-value redaction (P5)
  _adapters.py     type adapters (numpy/pandas/…)
  _viewer.py       the Textual TUI app (Phase 1.5)
  _viewer_model.py rendering-free viewer logic (inline values, aliases)
  _repro.py        crash → self-contained verified reproduction (Phase 3, rung 1)
  _nondet.py       deterministic record/replay of non-determinism (Phase 3, rung 2)
  _read.py, _cli.py, _config.py
tests/             Python tests (serializer, capture, reader, CLI)
scripts/bench.py   overhead baseline
```

## License

MIT.
