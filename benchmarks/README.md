# Benchmarks

Reproducible measurements behind the project's core claim — *cheap enough to leave on in production*.
Numbers track your hardware; re-run them.

## `web_overhead.py` — overhead under real web load

A **Flask app served by waitress**, driven by concurrent HTTP clients, measuring end-to-end request
latency (p50/p90/p99) and throughput with the recorder **off**, **on** (default call/return
granularity), and **on with per-line** recording.

Methodology, so the number means something:

- The **server runs in a separate process** from the load driver, so the client is never itself
  recorded — only the app process carries Flight.
- Flight records **only the app's own handler code**; the framework (Flask/waitress, in site-packages)
  is excluded by default, exactly as in production. The overhead therefore scales with how much of *your*
  code runs per request.
- The handler does representative work (builds and summarises N records per request), so per-event cost
  actually accumulates instead of hiding behind a trivial `return "ok"`.
- Latencies are wall-clock per request across all clients; warmup requests are discarded.

```console
pip install pyflight flask waitress
python benchmarks/web_overhead.py
python benchmarks/web_overhead.py --concurrency 16 --requests 12000 --n 80 --threads 16
```

Flags: `--concurrency` (parallel clients), `--requests` (measured requests), `--warmup`, `--n` (records
built per request = handler work), `--threads` (waitress workers), `--port`. The script prints a table
and a machine-readable JSON block.

Reference run (Python 3.13, Linux; 8 threads; 8 clients; 12,000 requests; n=60):

| mode | throughput | p50 | p99 |
|---|---:|---:|---:|
| off (baseline) | 1240 req/s | 5.55 ms | 16.23 ms |
| on — default | 1221 req/s (98%) | 5.61 ms (1.01×) | 16.25 ms (1.00×) |
| on — per-line | 1050 req/s (85%) | 6.53 ms (1.18×) | 19.14 ms (1.18×) |

Default black-box mode is within measurement noise at p50/p99; per-line is a tail cost you pay only while
investigating.

## `../scripts/bench.py` — per-event cost (microbenchmark)

The steady-state per-event floor (`~65 ns` for a fully recorded event, `~40 ns` of which is
`sys.monitoring` itself). Build release first — a debug build is ~10× slower on the hot path:

```console
maturin develop --release
python scripts/bench.py
```
