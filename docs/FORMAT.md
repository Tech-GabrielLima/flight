# The `.flight` file format — v1

The `.flight` file is the only contract between the recording engine and every reader. It is designed
around one hostile constraint: **the process that writes it may die at any instant**, mid-write, and
the file must still be as useful as the bytes that made it to disk allow.

Three rules follow, and they are absolute:

1. **Append-only.** Every byte is written forward. The reader never depends on the writer having
   finished cleanly.
2. **Truncation-tolerant.** A reader parses as far as the bytes make sense and reports the file as
   `partial`, instead of failing.
3. **Forward-compatible.** Unknown block types are skipped, never errors. *New readers read old
   files; old readers survive new files.*

## Layout

```text
┌─────────┐
│ HEADER  │  magic "FLGT" | u16 LE format version | u32 LE meta_len | msgpack(meta)
├─────────┤
│ BLOCK   │  u8 type | u32 LE payload_len | zstd(msgpack(payload))
│ BLOCK   │
│  ...    │
├─────────┤
│ FOOTER  │  (optional) an INDEX block (type 0x70), then:
│         │  u32 LE index_block_total_len | trailer magic "TLGF"
└─────────┘
```

- The **header meta** is msgpack *uncompressed* and encoded as a **named map**, so any tool can sniff
  a file cheaply and tolerate new metadata fields appearing over time.
- Each **block payload** is msgpack, then zstd (level 3). High-volume payloads (events, objects) use
  compact *positional* msgpack arrays; metadata-ish payloads that must grow use *named* maps.
- The **footer** exists only when the writer closes cleanly (`FlightWriter::finish`). A crashing
  writer simply never emits it — the file is then footer-less but whole.

## Reading strategy

1. **Trailer fast path.** If the last 4 bytes are `TLGF`, seek to the INDEX block via the length just
   before the magic and load blocks by offset. Any inconsistency (bad offset, type mismatch, corrupt
   index) silently falls back to:
2. **Linear scan.** Walk blocks from the end of the header until EOF, a valid trailer, or the first
   byte that stops making sense (truncation / corruption). Everything parsed before that point is
   served; the file is flagged `partial` if the walk didn't end cleanly.

The INDEX block is a footer, never content — it never appears in the reader's block list on any path.

## Block types

The numeric ids are part of the format and are **never renumbered**. Ids for future phases are
reserved now, so the Phase-1.5 viewer can gain Phase-2 powers without a format rewrite (P3).

| ID | Block | Phase | Contents |
|------|-------------|:---:|---|
| 0x01 | META | 0 | environment: python version, platform, argv, cwd, flight version |
| 0x02 | SOURCE | 1 | source of the files involved (hash + text), for off-machine viewing |
| 0x03 | EXCEPTION | 1 | type, message, `__cause__` / `__context__` chain |
| 0x04 | FRAME | 1 | function, file, line, references to locals |
| 0x05 | OBJECT | 1 | serialized object graph (identity-preserving) |
| 0x06 | EVENT_RING | 0 | the last N execution events before death, merged across threads |
| 0x07 | MUTATION | 2 | one state write: who, what, new value, where, when |
| 0x08 | TIMELINE | 2 | checkpoints for efficient time navigation |
| 0x09 | NONDET | 3 | recorded sources of non-determinism (I/O, time, random) |
| 0x70 | INDEX | 0 | footer: index of all blocks, written on clean close only |
| 0x7F | EXT | — | extension space; unknown to a reader ⇒ skipped |

A Phase-0 file contains **META** and **EVENT_RING**. A Phase-1 crash file adds **EXCEPTION**,
**FRAME**, **OBJECT** and one **SOURCE** block per file involved. A Phase-2 scope file
(`with flight.record()`) contains **META**, **MUTATION**, **SOURCE** and **EVENT_RING**.

### MUTATION payload

A `Vec<Mutation>` in `seq` (logical) order — the event-sourcing log of a scope. Each record is
`(seq, kind, name, key, value, file, qualname, line, frame)` where `kind` is `"local"` (a variable was
(re)bound), `"item"` (a container key/index was written) or `"attr"` (an attribute was set), and
`value` is a *shallow* rendering `(kind, repr, type_name, length)` — a snapshot of what the value was,
not a deep graph, which is exactly what a per-variable history needs. Replaying the log answers "what
was `x` at step t" and "who wrote this key"; `frame` disambiguates recursion.

### FRAME / OBJECT payloads

Frames are ordered crash-first (frame 0 raised the exception). Each frame's locals map a name to an
object-graph node id; two frames sharing an object point at the *same* id — that is how aliasing is
recorded. Object nodes are flat records `(id, kind, repr, type_name, length, truncated, items)` where
`items` are `(key_or_null, child_id)` edges, so the graph is reconstructed by id and cycles are just
back-edges. `length` keeps the real size when a container/string was truncated to the capture limits.

### EVENT_RING payload

```text
{
  codes:  { code_id (u64) -> { file, qualname, first_line } },  # only referenced codes
  events: [ { kind, thread, line, code_id, tstamp }, ... ],     # ascending tstamp
  wrapped: bool                                                  # true if older events were dropped
}
```

`tstamp` is a **logical** clock (a global atomic counter), not wall time — it orders events across
threads at no clock cost. Each event is a fixed 24-byte record. `kind` is one of `PY_START`,
`PY_RETURN`, `LINE`, `RAISE`, `RERAISE`, `PY_UNWIND`.

## Guarantees, tested

The round-trip and tolerance rules are enforced by `crates/flight-reader/tests/roundtrip.rs`, which
among other things **truncates a complete file at every single byte offset** and asserts the reader
never panics and degrades monotonically. That test suite is what "the format is the spine" means in
practice.
