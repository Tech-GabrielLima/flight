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

## Status — Phase 6 (debugging by comparison) ✅

Every phase through 6 is complete, end to end and fully tested: 0 (foundation), 1 (the full black box),
1.5 (the TUI viewer), 2 (scoped time-travel), 3 (rungs 1–2 of re-execution), 4 (deterministic I/O +
thread/asyncio scheduling — see [deterministic I/O](#deterministic-io--phase-4) below), 5 (the reverse
debugger + DAP — see [reverse debugging](#reverse-debugging--phase-5) below) and 6 (`flight diff` +
delta debugging — see [debugging by comparison](#debugging-by-comparison--phase-6) below).

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

Honest scope (rung 3): the clock / randomness / uuid class — flaky tests, time bombs, "fails 1% of the
time" — is covered, and so now are files, pipes, subprocess, sockets and the asyncio/thread schedule
(Phase 4, below). What's still out: data races on *unlocked* shared state, and multiprocessing.

### Deterministic I/O — Phase 4

Scalar boundaries (clock/random/uuid) are only half of what makes a program non-deterministic; the rest is
**what it read from the world**. `flight.deterministic()` now records that too — file reads, `os.read`
pipes and subprocess output — as more entries on the same tape, so an I/O-dependent run replays **offline,
bit-for-bit**, even on a machine that no longer has those files:

```python
import flight

def load():
    with open("config.json") as f:      # a file read…
        cfg = f.read()
    out = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True)
    return cfg, out.stdout               # …and a subprocess: both recorded

with flight.deterministic("run.flight"):
    original = load()

# On another machine, with no config.json and no git — it still repeats:
assert flight.replay("run.flight", load) == original
```

Reads are keyed by an open-order **channel id**, so several files read interleaved never cross wires. On
replay, reads come from the tape and **writes are swallowed** — replaying a run that wrote to disk never
touches the disk. **Record what was read, hash the rest:** a read larger than `io_hash_above` bytes
(default 256 KiB) is stored as its length + a BLAKE2b digest instead of its content, keeping the `.flight`
tiny; on replay the live source is re-read and **verified** against the digest (a changed or missing source
raises `ReplayDivergence`). Pass `io_hash_above=0` to inline everything for fully offline replay.

Socket reads (`recv`/`recv_into`) are recorded the same way. For **asyncio**, the task-**completion order**
is recorded and checked on replay; since determinism comes from replaying time and I/O, this pinpoints any
residual scheduling divergence at the task level.

**Threads — the flaky "which thread won" bug.** Under the GIL, what still varies run to run is the *order
threads acquire shared locks*, which is what makes a lock-protected structure's contents non-deterministic.
flight records that order and **enforces it** on replay, so the interleaving repeats bit-for-bit:

```python
log, lock = [], threading.Lock()

def worker(name):
    for i in range(6):
        time.sleep(0.001)          # a real race, different every run
        with lock:
            log.append((name, i))  # who appends when? — non-deterministic

with flight.deterministic("run.flight"):
    run_three_workers()            # records the lock-acquisition schedule

# Every replay reproduces the *exact* recorded interleaving:
assert flight.replay("run.flight", run_three_workers) == recorded_log
```

Threads are numbered in start order, and each thread replays its own boundary calls on its own tape lane
(so two threads reading the clock never fight over one order). Honest limits: only locks your code creates
inside the scope are tracked (the runtime's own locks are left intact), non-blocking/timed acquires aren't
ordered, and data races on *unlocked* state are outside any lock-based record/replay. A safety timeout
turns a replay deadlock into a `ReplayDivergence`, never a hang.

### Reverse debugging — Phase 5

Phase 2 records every state write of a `with flight.record()` block with its exact line and sequence
number. Phase 5 turns that into a **reverse debugger**: a cursor you can step **backward** through, and a
**breakpoint in the past** — "jump to the write where `running` first passed 100":

```python
tt = flight.time_travel("scope.flight")   # starts at the end (post-mortem stance)
step = tt.find_first("running > 100")      # the breakpoint in the past
print(step.describe(), tt.state()["locals"]["running"])   # → the write, and the value there
tt.step_back(); tt.step_forward()          # walk the timeline
```

`state()` reconstructs the locals **and** watched-container contents at the cursor (event sourcing). The
condition parser is safe (no `eval`; an invalid comparison never crashes the session, P1), and there are
line breakpoints and watchpoints with `continue_forward` / `continue_back`.

**In your editor, for free.** `flight debug scope.flight` starts a **DAP** (Debug Adapter Protocol) server
on stdio. Because the adapter advertises `supportsStepBack`, VS Code and PyCharm show the **Step Back** and
**Reverse** buttons and drive them against the recording — locals, the mutation timeline and the past
breakpoint all navigable, no live process. Or answer a query straight from the shell:

```console
$ python -m flight debug scope.flight --find "running > 100"
first match: #10 tt.py:5 local running = 136
  state there: it=120, running=136
```

Honest scope: it operates on scope recordings (Phase 2's mutation timeline), at line granularity;
sub-line detail awaits the native bytecode instrumentation ([TECHNICAL.md](TECHNICAL.md) §3.2).

### Debugging by comparison — Phase 6

Two recordings of the "same" program — one that worked, one that failed — carry *why* between them: the
**first point they diverged**. `flight diff` aligns them position by position and reports it, on the
richest axis the two files share:

```console
$ python -m flight diff run_ok.flight run_fail.flight
comparing nondets: diverged at step 7 (12 steps compared)
  random.random answered differently
  left : random.random [f] 0.8313
  right: random.random [f] 0.1174
```

For scope recordings it's the first mutation whose target or value differs; for deterministic tapes it's
the first boundary call that answered differently (a *source* mismatch means control flow branched — the
root of a flaky test); otherwise the first ring event that took a different path. `flight diff` exits
non-zero when they differ, like `diff(1)`, so it drops into CI.

**Delta debugging — the minimal reproducer.** A deterministic `.flight` may hold hundreds of recorded
values, but usually only a few *cause* the failure. `flight.minimize` runs Zeller's **ddmin** over the
tape — replaying with more and more values replaced by a neutral default, keeping only the reductions that
still fail — until every remaining original value is load-bearing:

```python
res = flight.minimize("crash.flight", work)   # work: the same fn you'd replay
print(res.render())
# minimal reproducer: 1 of 6 recorded values matter (5 neutralized)
#   #3 random.randint [i] = 95      ← the only value your bug actually needs
```

Neutralizing a value that changes control flow makes the replay diverge — read as "didn't reproduce" — so
that value is correctly kept. The generic `ddmin` is a pure function, unit-tested on its own.

## Roadmap ahead — Phases 4–10

The compass: **fidelity → experience → intelligence → reach**. Every phase keeps the five inviolables
(P1–P5) and stays useful on its own (P4). Full contracts live in [VISION.md](VISION.md) §5.6.

- **Phase 4 — Total replay fidelity (close rung 3). ✅ Done.** Files, pipes, subprocess, sockets and the
  asyncio/thread schedule are now recorded and replayed offline (see [deterministic I/O](#deterministic-io--phase-4)
  below). The key trick for concurrency: record the *order* of lock acquisition (threads numbered in start
  order), not the scheduler's internals, and enforce it on replay — a program is a deterministic function
  of its inputs *and the order the world answered in*. Honest limits: only user-created locks are tracked,
  timed/non-blocking acquires aren't ordered, and data races on unlocked state are out of scope;
  multiprocessing is future work.
- **Phase 5 — A real reverse debugger. ✅ Done.** **Step-backward** and a "breakpoint in the past" ("jump
  to where `running` passed 100") over the mutation timeline, exposed over **DAP** (`supportsStepBack` →
  Step Back / Reverse in VS Code and PyCharm) and on the CLI (`flight debug`). See
  [reverse debugging](#reverse-debugging--phase-5) above. Sub-line granularity (native bytecode, TECHNICAL
  §3.2) is future work.
- **Phase 6 — Debugging by comparison. ✅ Done.** `flight diff run_ok.flight run_fail.flight` points at the
  **first diverging mutation / boundary call / event**; plus **delta debugging** (`flight.minimize`, ddmin
  over the tape) shrinking a crash to its minimal reproducer: "your bug needs only these 3 of the 500
  recorded values." See [debugging by comparison](#debugging-by-comparison--phase-6) above.
- **Phase 7 — The intelligence layer.** A `.flight` is the perfect structured context for an LLM.
  `flight explain` (root cause + suggested patch), `flight repro --pytest` (a committable regression test),
  semantic queries over the timeline, and Sentry-style dedup by **common frame + state**.
- **Phase 8 — The production black box.** An **adaptive overhead governor** (overhead as an SLO, not a bet),
  an **always-on daemon** that flushes on `SIGKILL`/OOM via an external supervisor (a black box that
  survives the plane), and **distributed correlation** (OpenTelemetry `traceparent`) for cross-service crashes.
- **Phase 9 — The viral loop & ecosystem.** A **browser viewer** (the Rust reader compiled to **WASM**), a
  **pytest plugin**, a **GitHub Action**, web-framework middleware, **cross-language recorders** writing the
  same `.flight`, and optional **at-rest encryption**.
- **Phase 10 — Moonshot: what-if debugging.** Edit a value in the past and re-execute forward over the
  deterministic tape: "what if `numbers` weren't empty here?" — the counterfactual result. Event sourcing +
  tape make it feasible.

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
python -m flight debug scope.flight       # reverse debugger: DAP server (VS Code / PyCharm)
python -m flight debug scope.flight --find "running > 100"   # a breakpoint in the past, on the CLI
python -m flight diff run_ok.flight run_fail.flight          # first point two runs diverged
```

Configuration (`flight.Config`): `ring_capacity`, `output_dir`, `dump_on_crash`, `record_lines`,
`record_returns` (see below), the `deny_prefixes` / `force_include` policy that keeps stdlib and
site-packages out of the recording, and the crash-capture budget (`capture_deadline_ms`,
`capture_max_bytes`, `max_str`, `max_container`, `max_depth`, `repr_limit`, `scrub_patterns`).

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

The other lever is **event volume**, not per-event cost. `record_returns=False` drops the PY_RETURN
events (the call path stays fully visible through PY_START, and returns are inferable) — that **halves**
the events on call-heavy code, taking the pathological case from ~2.5x to ~1.7x. It changes nothing
about crash capture, scope time-travel or replay; it's a pure knob, on by default.

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
  _io.py           deterministic I/O: file/pipe/subprocess/socket reads, hash-of-rest (Phase 4)
  _asyncio.py      asyncio task-completion order record + replay check (Phase 4)
  _threads.py      thread lock-acquisition order record + replay enforcement (Phase 4)
  _timetravel.py   reverse-debugger engine: cursor, breakpoint-in-the-past (Phase 5)
  _dap.py          Debug Adapter Protocol server over the engine (Phase 5)
  _diff.py         flight diff: first divergence of two recordings (Phase 6)
  _ddmin.py        delta debugging (ddmin) → minimal reproducer (Phase 6)
  _read.py, _cli.py, _config.py
tests/             Python tests (serializer, capture, reader, CLI)
scripts/bench.py   overhead baseline
```

## License

MIT.
