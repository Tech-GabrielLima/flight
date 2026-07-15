<div align="center">

<h1>✈&nbsp;&nbsp;flight</h1>

<h3>When a Python program dies, get the <em>black box</em> — not just a traceback.</h3>

<p>
A post-mortem recorder you can actually leave on. A lock-free ring buffer and the <code>.flight</code>
writer live in <b>Rust</b> (via PyO3), fed by CPython's <b><code>sys.monitoring</code></b> (PEP 669) so
steady-state overhead stays low. When a crash escapes, it flushes a self-describing,
truncation-tolerant <b>black box</b> you can open, share, and replay in time.
</p>

<p>
<a href="https://pypi.org/project/pyflight/"><img alt="PyPI" src="https://img.shields.io/pypi/v/pyflight?logo=pypi&logoColor=white&label=pypi&color=3775A9"></a>
<img alt="Python 3.12+" src="https://img.shields.io/pypi/pyversions/pyflight?logo=python&logoColor=white">
<a href="https://github.com/Tech-GabrielLima/flight/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/Tech-GabrielLima/flight/actions/workflows/ci.yml/badge.svg"></a>
<img alt="Rust + Python" src="https://img.shields.io/badge/core-rust%20%2B%20python-000?logo=rust&logoColor=white">
<img alt="MIT" src="https://img.shields.io/badge/license-MIT-3fb950">
</p>

<br>

<img alt="flight inspect diagnosing a crash: the black box points straight at the empty list" width="720"
     src="https://raw.githubusercontent.com/Tech-GabrielLima/flight/main/assets/demo.gif">

<br><br>

<pre><b>pip install pyflight</b></pre>

<sub><a href="README.pt-BR.md">Português</a> · <a href="VISION.md">Vision</a> · <a href="TECHNICAL.md">Technical guide</a> · <a href="docs/FORMAT.md">The .flight format</a></sub>

</div>

---

## The 60-second tour

Leave it on; let a crash write its own report. No breakpoints, no "can you reproduce it?"

```python
import flight
flight.install()          # cheap enough to leave on — see Overhead
...                       # run your program
# on an uncaught exception, a .flight is written automatically
```

```console
$ python -m flight run yourscript.py       # or just import flight; flight.install()
...
ZeroDivisionError: division by zero
[flight] recorded flight-57275.flight

$ python -m flight inspect flight-57275.flight
exception   : ZeroDivisionError: division by zero
frames      : 4 (crash first)
  #0 compute_average  (crash.py:26)
        numbers = list[0] ↔          # empty! this is the bug
  #1 summarize  (crash.py:32)
        data = list[0] ↔             # the SAME empty list, aliased in
```

The `↔` marks an object that is the *same* across frames: `data` (the empty `evening` dataset) **is**
`numbers` inside `compute_average`. The black box diagnosed the bug — the object graph, every frame's
locals, the exception chain and the source are all in the file, and it never had to run twice.

## What you can do with a `.flight`

| Command | What it answers |
|---|---|
| `flight inspect` | the crash: frames, locals, object graph, aliasing (`↔`) |
| `flight why --var x` | **why is this value what it is?** — a dynamic backward slice |
| `flight diff a b` | the first point two runs diverged (`--html` for a shareable page) |
| `flight bisect` | which commit introduced the bug (by fingerprint, or by replay) |
| `flight generalize` | the boundary at which a recorded value flips the failure |
| `flight fix` | propose **and verify** a patch by replaying it over the recorded tape |
| `flight view --serve` | what-if in the browser: change a past value, see the new outcome |
| `flight serve` | fleet mode: a dashboard aggregating thousands of black boxes |
| `flight repro [--pytest]` | a self-verifying reproduction script / regression test |

<sub>Recordings open with **no install** in a single offline HTML page — the Rust reader compiled to WebAssembly. Drop a `.flight` on it and read the crash in your browser.</sub>

---

## How it works

One path in, one file out, many ways to read it.

```text
   your program
        │   sys.monitoring (PEP 669) — native Rust callbacks, no Python frame, no FFI hop
        ▼
┌──────────────────────── in-process · hot path · Rust ────────────────────────┐
│  "is this code mine?"          lock-free per-thread          global logical    │
│   thread-local cache   ──▶      ring buffer (24-byte    ──▶     clock (merges   │
│   (no lock, ~25 ns)             push, no mutex)                 threads in time)│
└───────────────────────────────────────┬──────────────────────────────────────┘
                                         │  uncaught exception  ·  or capture()
                                         ▼
        object graph (identity-preserving, aliasing ↔)  +  every frame's locals
              +  the exception chain  +  the source of each file involved
                                         │
                                         ▼
                              ┌───────────────────────┐
                              │      crash.flight      │   msgpack + zstd · versioned
                              │  a self-describing box │   truncation-tolerant · shareable
                              └───────────┬───────────┘
             ┌───────────────────────────┼───────────────────────────────┐
             ▼                            ▼                               ▼
      flight inspect              browser viewer (WASM)            why · diff · fix
      CLI · Textual TUI           open offline, nothing            bisect · generalize
                                  uploaded, no install             what-if · serve
```

Three stages, and the middle one is the whole point:

1. **Record cheap enough to leave on.** CPython's `sys.monitoring` calls straight into **native Rust**
   callbacks — no Python callback frame, no second FFI hop. The hot path takes no lock: a thread-local
   cache answers "is this my code?", and interesting events are pushed onto a **lock-free per-thread ring
   buffer**. A callback can return `DISABLE` so a cold location is never called again. That's the ~65 ns
   that lets it stay on (see [Overhead](#overhead--the-honest-picture)).
2. **On a crash, write a black box — not a trace.** The `excepthook` (or an explicit `capture()`) walks
   the live stack once and serializes the **object graph** identity-first, so the *same* object in two
   frames is recorded as one and marked `↔`; it captures every frame's locals, the exception chain, and
   the **source** of each file — so the values still make sense on another machine. Everything is bounded
   (depth, bytes, time) so a giant or hostile object can never hang or blow up the capture, and it can
   never crash the program it's recording.
3. **Read it anywhere.** The `.flight` is the spine: the CLI, the TUI, the offline **WASM viewer**, and
   every analysis (`why`, `diff`, `fix`, `bisect`, `what-if`, fleet mode) only ever speak to the file —
   never to a live process.

### Open it in a browser — nothing installed

The Rust reader compiles to **WebAssembly** inside one self-contained HTML page. Drop a `.flight` on it
and the crash is parsed **in your browser**, offline — nothing is uploaded. This is the shareable artifact
the whole project is built around: *"open this and you'll see everything."*

It isn't only a reader: it diagnoses the crash (a heuristic *how to fix*), runs **`why`** (a real backward
slice) and an object explorer **client-side**, and compares two runs. Serve it with `flight view --serve
crash.flight` and the *same* page gets a live **what-if** console and a self-verifying **`fix`**, backed by
the real deterministic replay — one UI, offline for reading, engine-backed for re-execution.

<div align="center">
<img alt="the browser WASM viewer: a real .flight parsed offline — the crash, a heuristic how-to-fix, and the highlighted source line"
     width="820"
     src="https://raw.githubusercontent.com/Tech-GabrielLima/flight/main/assets/viewer.png">
</div>

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

## Status — all phases complete (0–17) ✅

The whole roadmap is done, end to end and fully tested — including the **second act, Phases 11–17**
(see [the next leap](#the-next-leap--phases-1117) below): a dynamic **reverse slice** (`flight why` — why
is this value what it is?), **`flight bisect`** (which commit introduced the bug — passively from a corpus,
or actively by replaying against each commit), reproducer **generalization** (`flight generalize` — the
boundary at which a value flips the failure), a **visual diff** page (`flight diff --html`) plus viewer
deep-links, an **agentic fix that *proves* the patch** (`flight fix` — replays the tape with the fix applied
and shows the crash is gone with no divergence), **what-if in the browser** (`flight view --serve`), and
**fleet mode** (`flight serve` — a collector, index and dashboard aggregating thousands of black boxes with
regression detection).

Through Phase 10, the first moonshot: **what-if
debugging** (`flight.what_if` — edit a value in the past and re-execute over the deterministic tape to see
the counterfactual; see [what-if debugging](#what-if-debugging--phase-10) below). Phase 9 removed the
friction around the shareable `.flight`: a **browser WASM viewer**, a **pytest plugin**, **`flight ci`** + a
**GitHub Action**, framework-agnostic **WSGI/ASGI middleware**, **Go and Node recorders** writing the same
format, and at-rest **encryption** (see [the ecosystem](#the-ecosystem--phase-9) below).

Every phase through 8: 0 (foundation), 1 (the full black box),
1.5 (the TUI viewer), 2 (scoped time-travel), 3 (rungs 1–2 of re-execution), 4 (deterministic I/O +
thread/asyncio scheduling — see [deterministic I/O](#deterministic-io--phase-4) below), 5 (the reverse
debugger + DAP — see [reverse debugging](#reverse-debugging--phase-5) below), 6 (`flight diff` +
delta debugging — see [debugging by comparison](#debugging-by-comparison--phase-6) below), 7 (`explain`
/ `repro --pytest` / semantic queries / dedup — see [the intelligence layer](#the-intelligence-layer--phase-7))
and 8 (an overhead SLO governor, a black box that survives `kill -9`, and cross-service correlation — see
[the production black box](#the-production-black-box--phase-8)).

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
$ pip install 'pyflight[viewer]'
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

### The intelligence layer — Phase 7

A `.flight` is the perfect structured context for a model. `flight explain` has two halves: a
**deterministic, offline root-cause summary** (no model, no network), and an **LLM-ready prompt** that a
provider can turn into prose + a patch.

```console
$ python -m flight explain crash.flight
ZeroDivisionError: division by zero
  crashed in compute_average (crash.py:5)
  likely cause — suspicious state at the crash:
    • numbers (list[0]) is empty
    • total (0) is zero
  → a divisor is zero.
```

The model call is an injectable layer (`explain(path, provider=fn)`), so this is fully useful and tested
with no API key, and *becomes* an LLM explainer when you configure one (`--llm`, Anthropic via
`ANTHROPIC_API_KEY`); a model/network failure never breaks it (P1). `flight explain --prompt` prints just
the bundle to paste into any model.

**A bug report that becomes a permanent test.** `flight repro --pytest` reuses the verified reconstruction
(Phase 3) to emit a committable regression test — with the crash's inputs frozen — that also self-verifies:

```console
$ python -m flight repro crash.flight --pytest -o test_bug.py
wrote test_bug.py
  ✓ verified: it reproduces the same exception
$ pytest test_bug.py          # def test_regression(): with pytest.raises(...): ...
```

**Semantic timeline queries.** Ask a question of the recorded history — "when did `cache` pass 100
entries?" — with a size condition over the mutation timeline:

```console
$ python -m flight debug cache.flight --find "len(cache) > 100"
first match: #205 svc.py:6 item cache[100] = 10000
```

**Dedup by frame + state.** `flight fingerprint` is a stable short hash of the exception chain, each
frame's `(qualname, file, offset-in-function)` and the *kinds* of the crash-frame's locals — so the same
bug groups to one id (even when line numbers shift), and different bugs stay apart. Better than grouping by
stack alone.

### The production black box — Phase 8

Three things stand between a recorder that works on your laptop and one you leave *on* in production: a
predictable cost, surviving a death that runs no Python, and making sense across a fleet of services.

**Overhead as an SLO, not a bet.** The per-event cost is fixed and honest (~65 ns), but a recorded hot loop
can still multiply it into real latency. The governor makes overhead a target you set: it samples the
recorder's throughput on a background thread, estimates the fraction of wall-clock it is costing, and dials
the granularity down a rung when it breaches the ceiling — dropping line events first, then returns, never
below "which functions ran and how it unwound" — then climbs back when things quiet down.

```console
$ python -m flight run --slo 0.03 service.py      # keep recording overhead under 3%
```

**A black box that survives the death of the plane.** An uncaught exception is easy — the excepthook writes
the file. `SIGKILL`, the OOM killer and segfaults run *no* Python, so nothing in-process can react. Flight
keeps a checkpoint of the ring on disk (written atomically every interval) and a **supervisor subprocess**
that shares a pipe with its parent. A clean shutdown sends one byte and the supervisor discards the
checkpoint; any death that *doesn't* send it closes the pipe, the supervisor gets EOF, and it promotes the
last checkpoint into a `flight-killed-*.flight`. The recorder can die and you still get the black box.

```console
$ python -m flight run --daemon service.py        # survive kill -9 / OOM
```

**Cross-service crashes.** In a mesh, a crash in service B is half a story without the request in service A
that caused it — the same problem distributed tracing already solves. Flight stamps the **W3C trace
context** (`traceparent`, read from an OpenTelemetry span, the environment, or set explicitly) onto every
black box, plus explicit links to upstream `.flight`s. `flight trace` then groups a directory by `trace_id`
into the cross-service crash graph:

```console
$ python -m flight trace ./crashes
trace 4bf92f3577b34da6a3ce929d0e0e4736  (2 services)
    [gateway]  flight-gateway-…-.flight   — TimeoutError: upstream did not respond
    [checkout] flight-checkout-…-.flight  — KeyError: 'cart_id'
        ↳ links to flight-gateway-…-.flight [gateway]
```

All of Phase 8 is **pure Python** over the existing engine — correlation rides the NONDET tape, and the
granularity is retuned live through `sys.monitoring`, so no format or Rust change was needed. Honest scope:
the overhead estimate is a calibrated single-thread number (it over-counts on many cores, which is safe for
an SLO); the checkpoint is periodic, so a hard kill can lose up to one interval of the most recent events;
and it is a supervisor-over-a-checkpoint, not yet a shared-memory ring the supervisor reads live — that is
the noted future refinement.

### The ecosystem — Phase 9

Phase 9 is about removing the friction between a crash and a shared, understandable `.flight`. It is a
basket of independent integrations; two are shipped.

**A pytest plugin.** A failing test gives you a traceback; `pytest --flight` gives you its black box.
The plugin records each test and, on failure, writes a full-detail `.flight` named after the test — then
points at it in the failure report and the run summary, **with the one command to open it**. It is opt-in
and never changes a test's outcome.

```console
$ pytest --flight
...
FAILED test_orders.py::test_refund - IndexError: list index out of range
------------------------------- Flight recording -------------------------------
black box: .flight/test_orders.py_test_refund.flight
open:      python -m flight view --serve .flight/test_orders.py_test_refund.flight   # or: flight inspect …
$ flight view --serve .flight/test_orders.py_test_refund.flight   # the crash, why, what-if — in your browser
```

`--flight-dir=DIR` chooses where they go; `--flight-lines` records per-line; `--flight-all` also keeps the
passing tests. It registers as a `pytest11` entry point, so an installed Flight is discovered automatically.

**Encryption at rest.** A `.flight` is made to be shared, but even after scrubbing it holds real values.
`flight encrypt` seals one so you can hand the *bug* to a vendor without handing over the *data*: the key
comes from a passphrase via scrypt, the payload is sealed with AES-256-GCM (authenticated — a wrong
passphrase or any tampering is detected), in an envelope with its own magic so ciphertext is never mistaken
for a `.flight`.

```console
$ python -m flight encrypt crash.flight --passphrase "$KEY"   # → crash.flight.enc
$ python -m flight decrypt crash.flight.enc --passphrase "$KEY"
```

AES-GCM needs a real cipher, so this depends on the **`cryptography`** package
(`pip install 'pyflight[crypto]'`); without it the commands fail with a clear message rather than
obscurely (the scrypt KDF and the envelope framing are stdlib-only).

**A viewer in the browser — the growth loop.** The `.flight` is shareable, so it should be openable with
nothing installed. The Rust `flight-reader` is compiled to **WebAssembly** and dropped into a single
self-contained HTML page: drag a `.flight` on and it's parsed **in your browser**, offline, nothing
uploaded. The reader's zstd is a C dependency that can't target `wasm32`, so the decode path swaps to the
pure-Rust **`ruzstd`** behind a `pure-zstd` feature (the encoder stays on the native `c-zstd` feature); a
small `flight-wasm` crate exposes a **raw C ABI** (`alloc` / `parse` / `dealloc`), so the page needs no
wasm-bindgen, no bundler — just the standard `WebAssembly` API. `scripts/build-wasm.sh` inlines the `.wasm`
as base64 into [`viewer-wasm/index.html`](viewer-wasm/), which then works straight from `file://`.

**A `.flight` per HTTP 500.** `flight._web` provides **framework-agnostic** middleware — `FlightWSGI` (Flask,
Django, Pyramid…) and `FlightASGI` (FastAPI, Starlette, Quart…) — because it speaks the WSGI/ASGI protocols,
not any one framework's API. A request that raises leaves a full black box, tagged with the request's
`traceparent` (passed per-request, so concurrent requests never clobber each other), before the exception
reaches the framework's error handling.

**Root cause on a red CI — where debugging pain actually happens.** `flight ci` renders a Markdown
root-cause comment from a crash `.flight` (reusing the `explain` heuristics and the fingerprint); the
composite **GitHub Action** in [`.github/actions/flight`](.github/actions/flight) drops it into the job
summary and, optionally, a PR comment, and **uploads the `.flight` as an artifact**. The comment carries a
one-command way to open that artifact — `flight view --serve` — so a red build becomes *"download the black
box, open it, see the exact state that failed,"* with no change to anyone's habits. A copy-paste example
workflow lives in [`.github/workflows/flight-example.yml`](.github/workflows/flight-example.yml).

**The format is language-agnostic — so recorders aren't Python-only.** [`recorders/go`](recorders/) and
[`recorders/node`](recorders/) write the **same** `.flight` format from Go and Node, read back unchanged by
the Rust/Python reader (`flight inspect`, the WASM viewer, everything). Both are **dependency-free**: a tiny
hand-rolled msgpack encoder, and a "stored" zstd frame (a valid zstd stream of raw, uncompressed blocks) so
no compressor is needed. One black-box format across a polyglot system.

### What-if debugging — Phase 10

The moonshot every earlier phase was building toward: **edit a value in the past and re-execute forward** to
see the counterfactual. Because Flight can hold the whole recorded world constant (the deterministic tape —
time, random, uuid, I/O) it runs the same function twice over the same tape: a **baseline** replay that
reproduces the recorded outcome bit-for-bit, and a **counterfactual** replay with one local variable
overwritten the moment control reaches a chosen line.

```python
# The recorded run crashed because `data` was empty. What if it weren't?
wi = flight.what_if("run.flight", compute, flight.Override("data", [2, 4], line=42))
print(wi.render())
# what-if:
#   · data := [2, 4] at line 42 (was [])
#   before: raised ZeroDivisionError: division by zero
#   after:  returned 2067.0
#   → the change alters the outcome.
```

Overwriting a live local needs no bytecode surgery on Python 3.13+, where `frame.f_locals` is a write-through
proxy (PEP 667). Three honest outcomes fall out: the run **returns/raises** something different (the
counterfactual), it **diverges** from the tape (the change would take a different path through the recorded
world — e.g. one more `random()` call — itself a finding), or it **never reaches** the override point
(reported, not ignored). The recorded non-determinism is held constant, so the counterfactual is
reproducible. It's API, like `minimize`, and needs 3.13+ (it says so when it can't apply an override).

## The next leap — Phases 11–17

The second act builds on the same blocks (the event ring, frames, the object graph, mutations, the NONDET
tape) and the reader's query surface — no `.flight` format change through Phase 16; only Phase 17 adds an
index *outside* the file. Every piece declares its honest limits.

### Dynamic reverse slice — Phase 11

Point at a value and ask **why it is what it is**. `flight why` builds the minimal chain of writes and
aliasings that produced it, receding to the origin — Weiser's backward slice, but from what *actually
happened*. The identity-preserving object graph hands two **exact** edges for free: **aliasing** (the same
object under another name/frame) and **containment** (the value *is* `datasets['evening']`); scope
recordings add exact **write** edges; a crash-only fallback follows name-level **read-of** edges from the
source AST, pruned by the event ring (a safe superset — never loses the cause).

```console
$ python -m flight why crash.flight --var numbers
numbers ([]) — how this value came to be:
  #0 compute_average  crash.py:26     parameter — its value comes from the caller (aliased below)
       ↔ the SAME object as 'data' in summarize (frame #1)
       ↰ is datasets['evening'] in main (frame #2)  (dict[3])
  ⇒ root: 'datasets' was dict[3] in main (frame #2)
```

### `flight bisect` — Phase 12

Which commit introduced the bug? **Passive** mode groups a corpus of black boxes by
[`fingerprint`](#the-intelligence-layer--phase-7) and reports the earliest commit a fingerprint appears at
(the commit rides the NONDET tape when you record with `flight.install(commit=True)`). **Active** mode
generates a harness from the crash once, then binary-searches `good..bad`: at each commit it checks out the
code into a throw-away `git worktree` and replays the recorded inputs+tape against **that commit's code** —
reproduces → *bad*, clean → *good*, build/resolve failure → *skip*, exactly like `git bisect`.

```console
$ python -m flight bisect ./crashes --fingerprint 8f3a1c        # passive
$ python -m flight bisect --repro crash.flight --good v1.2 --bad HEAD   # active
culprit: e5f6a7b "off-by-one in refund()"  (14 commits tested)
```

### Reproducer generalization — Phase 13

`ddmin` (Phase 6) isolates *which* recorded values matter; `flight generalize` finds the **boundary** at
which each one flips the failure on and off — an exponential probe outward from the recorded value to a
*passing* value, then a bisection to the exact transition. It emits a candidate guard and a Hypothesis
scaffold; the passing example is verified by construction (replaying with it doesn't reproduce).

```console
$ python -m flight generalize crash.flight --property
  random.randint → fails when ≥ 95 (passes at 94)
     ⇒ candidate property: `assert n < 95` would guard the bug
```

### Visual diff + deep-links — Phase 15

`flight diff --html a.flight b.flight -o diff.html` renders a **self-contained** side-by-side diff page (no
external assets — attach it to a PR/issue) with the divergence point the CLI reports highlighted. The browser
WASM viewer gains a **compare mode** (drop two files → the first divergence on the event axis, highlighted)
and **URL-fragment deep-links** ("copy link to this view") — the link references a view, never the data (P5:
the `.flight` never travels in the URL).

### The agentic fix that *proves* the patch — Phase 16 (capstone)

`flight fix` navigates the recording through read-only tools (frames, locals, aliases, the Phase-11 slice),
proposes a **patch** (a unified diff against the embedded source), and then Flight **re-executes the
deterministic tape with the patched code** to decide the verdict: crash gone **and** no boundary divergence →
`VERIFIED`; crash gone **but** the tape diverges → `CHANGES_BEHAVIOR`; crash persists → `REJECTED` (the
traceback is fed back for another attempt). This "does not reproduce **and** does not diverge" proof needs
deterministic replay + a verifiable repro + the embedded source in one artifact — the convergence of Phases
3–7. A deterministic heuristic provider proposes *and verifies* patches with no API key; `--llm` adds an
Anthropic provider. The agent's tools are strictly read-only (P1/P3).

```console
$ python -m flight fix crash.flight
  +    if not numbers:
  +        return 0.0
verification over the recorded tape:
  ✓ the crash no longer reproduces
  ✓ no boundary divergence (time/random/IO identical)
  ⇒ FIX VERIFIED
```

### What-if in the browser — Phase 14

`flight view --serve crash.flight` serves a **what-if console**: list the crash-frame locals, change one at a
line, and see the real counterfactual — the Phase-10 `what_if` engine (with the function resolved in-process
from the embedded source) run behind a `POST /whatif` endpoint. The recorded world is held constant, so the
outcome is reproducible; it degrades honestly to "needs 3.13+" when a live-local override can't apply.

### Fleet mode — Phase 17

`flight serve ./store` runs a **collector + index + dashboard** aggregating thousands of black boxes:
`POST /ingest` extracts `{fingerprint, exc_type, service, commit, trace_id}` into a SQLite index; the
dashboard shows top fingerprints ("4,213× since Tuesday, ↑ new since deploy-42"), regression detection
against recorded deploys, and the cross-service graph by `trace_id`. Sources report to it with a common
`report_to=` (the WSGI/ASGI middleware wires it), and a privacy linter (`safe_to_send`) refuses to `POST` a
black box with obvious unscrubbed secrets. It is the one phase that adds state *outside* the file — never
inside it.

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
- **Phase 7 — The intelligence layer. ✅ Done.** `flight explain` (offline heuristic root cause + an
  LLM-ready prompt / optional model call), `flight repro --pytest` (a committable, self-verifying
  regression test), semantic timeline queries (`len(cache) > 100`), and `flight fingerprint` dedup by
  **frame + state**. See [the intelligence layer](#the-intelligence-layer--phase-7) above.
- **Phase 8 — The production black box. ✅ Done.** An **adaptive overhead governor** (overhead as an SLO,
  not a bet), an **always-on daemon** that flushes on `SIGKILL`/OOM via an external supervisor (a black box
  that survives the plane), and **distributed correlation** (W3C `traceparent` / OpenTelemetry) for
  cross-service crashes. See [the production black box](#the-production-black-box--phase-8) above.
- **Phase 9 — The viral loop & ecosystem. ✅ Done.** A **browser viewer** (the Rust reader compiled to
  **WASM**, offline single-file page), a **pytest plugin** (`pytest --flight`), **`flight ci`** + a **GitHub
  Action** for red CI, framework-agnostic WSGI/ASGI **middleware**, **Go and Node recorders** writing the
  same `.flight`, and optional at-rest **encryption**. See [the ecosystem](#the-ecosystem--phase-9) above.
- **Phase 10 — Moonshot: what-if debugging. ✅ Done.** `flight.what_if` edits a value in the past and
  re-executes forward over the deterministic tape: "what if `numbers` weren't empty here?" — the
  counterfactual result, with the recorded world held constant. See
  [what-if debugging](#what-if-debugging--phase-10) above.

## Install

For everyone — a prebuilt wheel, no Rust toolchain needed:

```console
pip install pyflight
```

Wheels are published for Linux, macOS (Intel + Apple Silicon) and Windows. The
native core is built against CPython's **stable ABI (abi3)**, so **one wheel per
platform** covers Python 3.12, 3.13 and every later 3.x — you don't wait for a
new build when you upgrade Python. Optional extras: `pip install
'pyflight[viewer]'` (the Textual TUI), `[crypto]` (at-rest encryption).

### From source (contributors)

Requires Python **3.12+** and a Rust toolchain.

```console
python -m venv .venv && . .venv/bin/activate
pip install maturin pytest textual
maturin develop --release            # compiles the Rust core (release), installs editable
```

(Plain `maturin develop` builds Rust in debug mode — fine for iterating, ~10× slower on the hot path.
Use `--release` for the real numbers.)

### Publishing a release (maintainers)

CI ([`.github/workflows/release.yml`](.github/workflows/release.yml)) builds wheels for all
platforms and publishes them to PyPI on a version tag, authenticating with a PyPI
API token stored as the `PYPI_API_TOKEN` repository secret. Then:

```console
git tag v0.0.4 && git push origin v0.0.4     # → CI builds every wheel and uploads to PyPI
```

`workflow_dispatch` runs the same build as a dry run (wheels as artifacts, no publish).

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
python -m flight explain crash.flight                        # heuristic root cause (+ --llm / --prompt)
python -m flight repro crash.flight --pytest -o test_bug.py  # a committable regression test
python -m flight fingerprint crash.flight                    # dedup id (by frame + state)
python -m flight run --slo 0.03 --daemon service.py          # overhead SLO + survive kill -9 / OOM
python -m flight trace ./crashes                             # cross-service crash graph (by trace id)
python -m flight encrypt crash.flight --passphrase "$KEY"    # seal at rest (needs the [crypto] extra)
python -m flight ci .flight                                  # Markdown root-cause comment for CI
pytest --flight                                              # a .flight for every failing test (plugin)
```

For production (Phase 8): keep overhead under a ceiling, survive an uncatchable death, and correlate crashes
across services.

```python
flight.install(overhead_slo=0.03, daemon=True)   # governor + crash-surviving supervisor
flight.correlate(service="checkout")             # stamp the W3C trace context (env / OTel / explicit)
flight.link(upstream_flight_path)                # reference the upstream service's black box
```

Configuration (`flight.Config`): `ring_capacity`, `output_dir`, `dump_on_crash`, `record_lines`,
`record_returns` (see below), the `deny_prefixes` / `force_include` policy that keeps stdlib and
site-packages out of the recording, the crash-capture budget (`capture_deadline_ms`, `capture_max_bytes`,
`max_str`, `max_container`, `max_depth`, `repr_limit`, `scrub_patterns`), and the production knobs
(`overhead_slo`, `governor_interval`, `per_event_ns`, `daemon`, `daemon_interval`, `correlation`).

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

### Under real load — a web app, not a microbenchmark

Per-event nanoseconds are only convincing if they disappear into real request latency. So the repo ships
[`benchmarks/web_overhead.py`](benchmarks/web_overhead.py): a **Flask app served by waitress**, driven by
concurrent clients, measuring end-to-end **p50/p90/p99** with the recorder off, on (the default
call/return granularity), and on with per-line recording. The server runs in a separate process so the
load driver is never itself recorded, and — as in production — Flight records only the app's own handler
code (stdlib and site-packages are excluded by default).

Reference run (Python 3.13, Linux; Flask + waitress, 8 worker threads; 8 concurrent clients, 12,000
requests after 1,500 warmup; handler builds 60 records/request):

| mode | throughput | p50 | p90 | p99 |
|---|---:|---:|---:|---:|
| **off** (baseline) | 1240 req/s | 5.55 ms | 9.63 ms | 16.23 ms |
| **on** — default (call/return) | 1221 req/s (**98%**) | 5.61 ms (**1.01×**) | 9.86 ms | 16.25 ms (**1.00×**) |
| **on** — per-line (`record_lines`) | 1050 req/s (85%) | 6.53 ms (1.18×) | 11.34 ms | 19.14 ms (1.18×) |

**In the default "leave it on" mode the recorder is within measurement noise at p50 and p99** (~2%
throughput). Per-line recording — which you'd turn on only while actively investigating — costs ~18% at
the tail. Reproduce (numbers will track your hardware):

```console
pip install pyflight flask waitress
python benchmarks/web_overhead.py                       # default load
python benchmarks/web_overhead.py --concurrency 16 --requests 12000 --n 80
```

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
- **P2 — Honest, bounded overhead.** Black-box mode targets <5% overhead; with `overhead_slo` set the
  Phase-8 governor actively defends a ceiling by retuning granularity (overhead as an SLO, not a bet).
- **P3 — The `.flight` format is the spine.** Engine and viewer only speak through it; it's versioned
  from day one; new readers read old files, old readers skip unknown blocks.
- **P4 — Every phase is useful on its own.**
- **P5 — Privacy by design.** Redaction of sensitive fields is a Phase-1 feature, not a later patch.

## Reliability under violent death

The `.flight` format is designed to survive truncation — but *designed for* is not *tested under*. The
writer is **streaming and append-only** (`File::create`, then sequential blocks — no temp-and-rename), so
the bytes on disk after a process is killed mid-write are always a **prefix** of the file. Two harnesses in
[`benchmarks/fault_injection.py`](benchmarks/fault_injection.py) turn the robustness claim into an
auditable number. Every read runs in an **isolated subprocess**, so a reader crash (segfault, abort,
uncaught panic) surfaces as a non-zero exit — not a silent pass:

- **Real `kill -9` during the write.** A process records a large crash and writes it in a loop; the parent
  `SIGKILL`s it mid-write. Whatever landed on disk is then parsed.
- **Exhaustive truncation.** Real recordings (a crash, a deterministic run, a big crash) are fed to the
  reader at *every byte prefix* — the complete superset of every state any kill could leave.

Reference run (Python 3.13, Linux):

| harness | reads | reader crashes |
|---|---:|---:|
| `SIGKILL` mid-write (700 kills) | 457 on-disk files (456 complete, 1 partial, 41 empty) | **0** |
| truncation at every byte prefix | 7,957 prefixes (7,803 `partial`, 139 graceful errors) | **0** |
| **total** | **8,414** | **0** |

Every corrupt file either parsed, degraded to `partial`, or raised a graceful error — **the reader never
crashed**. A fast version runs in CI ([`tests/test_fault_injection.py`](tests/test_fault_injection.py)).
Reproduce with `python benchmarks/fault_injection.py`.

## Tests

**2161 Python tests** (2151 pass; 10 skip only for a missing optional dependency — `numpy`, `pandas` — or
the inverse crypto-absent path when `cryptography` *is* installed) and **377 Rust tests**, all green, at
**100% statement coverage**.

```console
cargo test                 # Rust — 377 tests across the three crates
pytest                     # Python — 2161 tests across every module
./scripts/coverage.sh      # Python coverage (100%, incl. subprocess-run code)
python scripts/bench.py    # steady-state overhead baseline
```

Every module has a dedicated file, heavily parametrized so each behaviour is checked across its real input
space rather than one happy path. The bulk (25 files):

| Area | Tests | What it exercises |
|---|--:|---|
| foundation (config / scrub / serialize / adapters) | 296 | object graph — cycles, aliasing, every budget/limit boundary, hostile `__repr__`; the deny/scrub policy |
| capture / read / install / cli / robustness | 354 | crash capture across exception kinds & chains; the reader accessors; install lifecycle; the CLI |
| record / timetravel / dap | 295 | scope mutation capture; the reverse-debugger cursor & query engine; the full DAP protocol |
| nondet / io / asyncio / threads | 293 | bit-for-bit replay of all 20 non-deterministic boundaries; file/pipe/subprocess/socket I/O; lock-order enforcement |
| diff / ddmin / explain / fingerprint / repro | 268 | first-divergence diff; ddmin minimality; heuristic root cause; fingerprint stability; verified repro |
| correlation / governor / daemon / crypto | 249 | W3C trace context; the overhead SLO state machine; the crash-surviving supervisor; AES-GCM at rest |
| pytest plugin / web / ci / whatif / viewer | 246 | `pytest --flight`; WSGI+ASGI 500s; the CI comment; what-if's outcome kinds; the rendering-free viewer model |
| polyglot interop (Go / Node / WASM) | 4 | Go & Node recorders and the WASM reader driven end to end (`go run`, `node`, a JS runtime) |
| coverage completion | 156 | targeted tests driving the last edge/error branches to 100% (defensive guards, subprocess paths, optional-dep paths) |

The marquee Rust test **truncates a valid `.flight` at every byte offset** and asserts the reader never
panics — it degrades to `partial` or errors cleanly.

**Coverage — 100%.** Measured with `coverage.py` over the whole package: **100% of statements, every module,
0 uncovered lines** (`./scripts/coverage.sh` — it runs the suite, merges per-process data, and reports).
Reaching it honestly took three things:

- **subprocess coverage** — code that only runs in a child process (`python -m flight run`, the pytest
  plugin hooks inside a spawned pytest, the crash daemon) is captured via a `coverage.process_startup()`
  hook, so it counts too, not just the parent process;
- **the optional dep** — `cryptography` installed so the AES-GCM path of `_crypto` is exercised (its
  *absence* path is covered by faking the import);
- **`# pragma: no cover` only where a line is genuinely unreachable in test** — the numpy/pandas/OpenTelemetry
  adapters (require those libraries), the `<3.12` interpreter guard, and a couple of P1 `except` guards that
  exist solely so the recorder can never crash the program it records and only fire on injected faults. Each
  is commented with why. Everything else is exercised by a real test.

Reaching it also surfaced real things, fixed at the source: the scrubber docstring now documents its
intentional over-redaction, and a stale decrypt-path test expectation was corrected. Design limitations
that are working-as-intended are pinned by tests asserting the real behaviour, so the suite is a faithful
description of what the code does.

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
  _explain.py      flight explain: heuristic root cause + LLM-ready prompt (Phase 7)
  _fingerprint.py  crash dedup fingerprint by frame + state (Phase 7)
  _governor.py     adaptive overhead governor: overhead as an SLO (Phase 8)
  _daemon.py       crash-surviving supervisor: flush a black box after SIGKILL/OOM (Phase 8)
  _correlation.py  W3C trace context + cross-service crash graph (Phase 8)
  _pytest.py       pytest plugin: a .flight per failing test (Phase 9)
  _crypto.py       at-rest encryption of a .flight (AES-256-GCM, optional) (Phase 9)
  _web.py          WSGI/ASGI middleware: a .flight per HTTP 500 (Phase 9)
  _ci.py           flight ci: a Markdown root-cause comment for CI (Phase 9)
  _whatif.py       what-if: re-execute the tape with a value changed (Phase 10);
                   run_whatif + the browser what-if console server (Phase 14)
  _slice.py        dynamic reverse slice: `flight why` (Phase 11)
  _bisect.py       `flight bisect`: passive corpus + active git-driven (Phase 12)
  _generalize.py   reproducer generalization: `flight generalize` (Phase 13)
  _agent.py        the agentic fix that proves the patch: `flight fix` (Phase 16)
  _fleet.py        fleet mode: collector + index + dashboard, `flight serve` (Phase 17)
  _read.py, _cli.py, _config.py
crates/flight-wasm/  the reader compiled to WebAssembly (raw C ABI) (Phase 9)
viewer-wasm/       self-contained offline browser viewer (built by scripts/build-wasm.sh)
recorders/go, recorders/node   cross-language recorders writing the same .flight (Phase 9)
.github/actions/flight          GitHub Action: root cause on a red CI (Phase 9)
tests/             Python tests (serializer, capture, reader, CLI, ecosystem, polyglot)
scripts/bench.py   overhead baseline · scripts/build-wasm.sh  build the WASM viewer
```

## License

MIT.
