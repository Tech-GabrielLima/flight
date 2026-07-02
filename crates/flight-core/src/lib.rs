//! `flight._core` — the native hot path of Flight, exposed to Python via PyO3.
//!
//! Responsibilities kept here (and nowhere in Python):
//! - the lock-free per-thread [`ring::Ring`] and the [`recorder::Recorder`]
//!   that owns the logical clock and the code map;
//! - writing the `.flight` file ([`dump`]);
//! - a small read summary for the CLI.
//!
//! Everything crossing the FFI obeys **P1 — primum non nocere**: a Rust panic
//! never unwinds into the interpreter. Hot-path callbacks (`record`,
//! `register_code`) swallow panics and return quietly; a corrupted recording
//! is acceptable, a crash caused by Flight is not.

mod dump;
mod recorder;
mod ring;

use std::panic::{catch_unwind, AssertUnwindSafe};
use std::path::PathBuf;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::OnceLock;

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList};

use flight_format::{
    EventKind, ExceptionLink, FrameInfo, MetaBlock, Mutation, MutationValue, NonDetEvent,
    ObjectItem, ObjectNode, SourceFile,
};
use flight_reader::FlightFile;
use recorder::Recorder;

/// `(filename, sha1, text)` as passed from Python.
type SourceTuple = (String, String, String);
/// `(exc_type, message, relation)`.
type ExceptionTuple = (String, String, String);
/// `(file, qualname, lineno, first_lineno, [(name, object_id)])`.
type FrameTuple = (String, String, u32, u32, Vec<(String, u64)>);
/// `(id, kind, repr, type_name, length, truncated, [(key, value_id)])`.
type ObjectTuple = (
    u64,
    String,
    Option<String>,
    Option<String>,
    Option<u64>,
    bool,
    Vec<(Option<String>, u64)>,
);
/// `(kind, repr, type_name, length)` — a shallow value rendering.
type ValueTuple = (String, Option<String>, Option<String>, Option<u64>);
/// `(seq, kind, name, key, value, file, qualname, line, frame)`.
type MutationTuple = (
    u64,
    String,
    String,
    Option<String>,
    ValueTuple,
    String,
    String,
    u32,
    u64,
);
/// `(seq, source, tag, payload)` — one recorded non-deterministic result.
type NonDetTuple = (u64, String, String, String);

/// The process-global recorder, built once on first use.
static RECORDER: OnceLock<Recorder> = OnceLock::new();
/// Ring capacity to use when the recorder is first built. Settable via
/// [`configure`] before the recorder exists.
static RING_CAP: AtomicUsize = AtomicUsize::new(4096);

fn recorder() -> &'static Recorder {
    RECORDER.get_or_init(|| Recorder::new(RING_CAP.load(Ordering::Relaxed)))
}

/// `sys.monitoring.DISABLE`, handed to us at configure time so the native
/// callbacks can return it without a Python attribute lookup on the hot path.
static DISABLE: OnceLock<Py<PyAny>> = OnceLock::new();

/// Record one execution event. Retained for the Python fallback path and
/// tests; the fast path is the native callbacks below.
#[pyfunction]
fn record(kind: u8, code_id: u64, line: u32) {
    let _ = catch_unwind(|| {
        if let Some(k) = EventKind::from_u8(kind) {
            recorder().record(k, code_id, line);
        }
    });
}

/// Install the deny/force policy and the `DISABLE` sentinel so the native
/// `sys.monitoring` callbacks can filter and record entirely in Rust.
#[pyfunction]
fn configure_filter(deny: Vec<String>, force: Vec<String>, disable: Py<PyAny>) {
    recorder().set_filter(deny, force);
    let _ = DISABLE.set(disable);
}

/// `None` to keep the event, or `sys.monitoring.DISABLE` to stop being called
/// at this location (the coverage.py trick — pay once, then nothing).
#[inline]
fn keep_or_disable(py: Python<'_>, disable: bool) -> Py<PyAny> {
    if disable {
        if let Some(d) = DISABLE.get() {
            return d.clone_ref(py);
        }
    }
    py.None()
}

// A lock-free, thread-local direct-mapped cache of the interesting decision, so
// the per-event hot path never touches the recorder's mutex. A hot loop calls a
// handful of code objects, so the cache hits ~100%; a miss (or a stale entry
// after `reset()` bumps the generation) falls back to the recorder, which stays
// the source of truth. Thread-local ⇒ no sharing ⇒ correct under free-threading.
const CACHE_SIZE: usize = 512;

#[derive(Clone, Copy)]
struct CacheSlot {
    code_id: u64,
    generation: u64,
    /// 1 = interesting, 2 = not; 0 = empty.
    state: u8,
}

thread_local! {
    static INTEREST: std::cell::UnsafeCell<[CacheSlot; CACHE_SIZE]> =
        const { std::cell::UnsafeCell::new([CacheSlot { code_id: 0, generation: 0, state: 0 }; CACHE_SIZE]) };
}

/// Decide whether `code` is interesting (cached per code id), registering it on
/// first sight. Returns its `code_id` if interesting, or `None` to DISABLE.
#[inline]
fn interesting_code_id(code: &Bound<'_, PyAny>) -> Option<u64> {
    let code_id = code.as_ptr() as u64;
    let rec = recorder();
    let generation = rec.generation();
    // Pointers are 16-byte aligned, so shift the low zero bits out of the index.
    let idx = ((code_id >> 4) as usize) & (CACHE_SIZE - 1);

    // Fast path: a lock-free thread-local hit.
    let cached = INTEREST.with(|c| {
        // SAFETY: thread-local, so this thread is the only accessor.
        let slot = unsafe { (*c.get())[idx] };
        if slot.code_id == code_id && slot.generation == generation {
            slot.state
        } else {
            0
        }
    });
    match cached {
        1 => return Some(code_id),
        2 => return None,
        _ => {}
    }

    let interesting = match rec.interesting_cached(code_id) {
        Some(v) => v,
        None => {
            let file = code
                .getattr("co_filename")
                .ok()
                .and_then(|f| f.extract::<String>().ok())
                .unwrap_or_default();
            let v = rec.decide_interesting(code_id, &file);
            if v {
                let qual = code
                    .getattr("co_qualname")
                    .ok()
                    .and_then(|q| q.extract::<String>().ok())
                    .unwrap_or_default();
                let first = code
                    .getattr("co_firstlineno")
                    .ok()
                    .and_then(|q| q.extract::<u32>().ok())
                    .unwrap_or(0);
                rec.register_code(code_id, &file, &qual, first);
            }
            v
        }
    };
    // Fill the thread-local slot for next time.
    INTEREST.with(|c| {
        // SAFETY: thread-local, single accessor.
        unsafe {
            (*c.get())[idx] = CacheSlot {
                code_id,
                generation,
                state: if interesting { 1 } else { 2 },
            };
        }
    });
    if interesting {
        Some(code_id)
    } else {
        None
    }
}

/// Record a filtered event; returns whether to DISABLE this location. Only for
/// events that support DISABLE (LINE, PY_START, PY_RETURN).
#[inline]
fn record_filtered(code: &Bound<'_, PyAny>, kind: EventKind, line: u32) -> bool {
    match interesting_code_id(code) {
        Some(code_id) => {
            recorder().record(kind, code_id, line);
            false
        }
        None => true,
    }
}

/// Record a filtered event without ever disabling — `sys.monitoring` forbids
/// returning DISABLE from RAISE / RERAISE / PY_UNWIND.
#[inline]
fn record_no_disable(code: &Bound<'_, PyAny>, kind: EventKind) {
    if let Some(code_id) = interesting_code_id(code) {
        recorder().record(kind, code_id, 0);
    }
}

/// Native `sys.monitoring` LINE callback — the hot path, called by the
/// interpreter directly (no Python frame, no second FFI hop).
#[pyfunction]
fn cb_line(py: Python<'_>, code: Bound<'_, PyAny>, line: u32) -> Py<PyAny> {
    let disable = catch_unwind(AssertUnwindSafe(|| {
        record_filtered(&code, EventKind::Line, line)
    }))
    .unwrap_or(false);
    keep_or_disable(py, disable)
}

/// Native PY_START callback.
#[pyfunction]
fn cb_py_start(py: Python<'_>, code: Bound<'_, PyAny>, _offset: Bound<'_, PyAny>) -> Py<PyAny> {
    let disable = catch_unwind(AssertUnwindSafe(|| {
        record_filtered(&code, EventKind::PyStart, 0)
    }))
    .unwrap_or(false);
    keep_or_disable(py, disable)
}

/// Native PY_RETURN callback.
#[pyfunction]
fn cb_py_return(
    py: Python<'_>,
    code: Bound<'_, PyAny>,
    _offset: Bound<'_, PyAny>,
    _retval: Bound<'_, PyAny>,
) -> Py<PyAny> {
    let disable = catch_unwind(AssertUnwindSafe(|| {
        record_filtered(&code, EventKind::PyReturn, 0)
    }))
    .unwrap_or(false);
    keep_or_disable(py, disable)
}

/// Native RAISE / RERAISE / PY_UNWIND callbacks (all carry an exception arg).
/// These events cannot be disabled, so they always return `None`.
#[pyfunction]
fn cb_raise(
    py: Python<'_>,
    code: Bound<'_, PyAny>,
    _offset: Bound<'_, PyAny>,
    _exc: Bound<'_, PyAny>,
) -> Py<PyAny> {
    let _ = catch_unwind(AssertUnwindSafe(|| {
        record_no_disable(&code, EventKind::Raise)
    }));
    py.None()
}

#[pyfunction]
fn cb_reraise(
    py: Python<'_>,
    code: Bound<'_, PyAny>,
    _offset: Bound<'_, PyAny>,
    _exc: Bound<'_, PyAny>,
) -> Py<PyAny> {
    let _ = catch_unwind(AssertUnwindSafe(|| {
        record_no_disable(&code, EventKind::Reraise)
    }));
    py.None()
}

#[pyfunction]
fn cb_unwind(
    py: Python<'_>,
    code: Bound<'_, PyAny>,
    _offset: Bound<'_, PyAny>,
    _exc: Bound<'_, PyAny>,
) -> Py<PyAny> {
    let _ = catch_unwind(AssertUnwindSafe(|| {
        record_no_disable(&code, EventKind::PyUnwind)
    }));
    py.None()
}

/// Register a code object's identity the first time it is seen. Returns
/// `True` if this was the first registration for `code_id`.
#[pyfunction]
fn register_code(code_id: u64, file: &str, qualname: &str, first_line: u32) -> bool {
    catch_unwind(|| recorder().register_code(code_id, file, qualname, first_line)).unwrap_or(false)
}

/// Set the ring capacity. Only effective before the recorder is first used;
/// returns `True` if it took effect, `False` if the recorder already exists.
#[pyfunction]
fn configure(ring_cap: usize) -> bool {
    if RECORDER.get().is_some() {
        return false;
    }
    RING_CAP.store(ring_cap, Ordering::Relaxed);
    true
}

/// Recorder counters, for `flight.stats()`.
#[pyfunction]
fn stats(py: Python<'_>) -> PyResult<Py<PyDict>> {
    let d = PyDict::new(py);
    let rec = recorder();
    d.set_item("total_events", rec.total_events())?;
    d.set_item("threads", rec.thread_count())?;
    d.set_item("codes", rec.code_count())?;
    d.set_item("ring_capacity", RING_CAP.load(Ordering::Relaxed))?;
    Ok(d.into())
}

/// Forget all recorded state.
#[pyfunction]
fn reset() {
    let _ = catch_unwind(|| recorder().reset());
}

/// Write the current recording to a `.flight` file at `path`.
#[pyfunction]
#[pyo3(name = "dump")]
#[allow(clippy::too_many_arguments)]
fn dump_file(
    path: PathBuf,
    python_version: String,
    platform: String,
    argv: Vec<String>,
    cwd: String,
    flight_version: String,
) -> PyResult<()> {
    let meta = MetaBlock {
        python_version,
        platform,
        argv,
        cwd,
        flight_version,
    };
    dump::dump(&path, meta, recorder()).map_err(|e| PyValueError::new_err(e.to_string()))
}

/// Write the full Phase-1 crash black box to `path`.
///
/// Data is passed as plain tuples that PyO3 converts automatically — the object
/// graph and frames are built in Python (the object walk runs once, in a doomed
/// process), and this only lays down the blocks. See [`dump::dump_crash`].
#[pyfunction]
#[pyo3(name = "dump_crash")]
#[allow(clippy::too_many_arguments)]
fn dump_crash(
    path: PathBuf,
    python_version: String,
    platform: String,
    argv: Vec<String>,
    cwd: String,
    flight_version: String,
    sources: Vec<SourceTuple>,
    exceptions: Vec<ExceptionTuple>,
    frames: Vec<FrameTuple>,
    objects: Vec<ObjectTuple>,
    nondet: Vec<NonDetTuple>,
) -> PyResult<()> {
    let meta = MetaBlock {
        python_version,
        platform,
        argv,
        cwd,
        flight_version,
    };
    let sources: Vec<SourceFile> = sources
        .into_iter()
        .map(|(filename, sha1, text)| SourceFile {
            filename,
            sha1,
            text,
        })
        .collect();
    let exceptions: Vec<ExceptionLink> = exceptions
        .into_iter()
        .map(|(exc_type, message, relation)| ExceptionLink {
            exc_type,
            message,
            relation,
        })
        .collect();
    let frames: Vec<FrameInfo> = frames
        .into_iter()
        .map(|(file, qualname, lineno, first_lineno, locals)| FrameInfo {
            file,
            qualname,
            lineno,
            first_lineno,
            locals,
        })
        .collect();
    let objects: Vec<ObjectNode> = objects
        .into_iter()
        .map(
            |(id, kind, repr, type_name, length, truncated, items)| ObjectNode {
                id,
                kind,
                repr,
                type_name,
                length,
                truncated,
                items: items
                    .into_iter()
                    .map(|(key, value_id)| ObjectItem { key, value_id })
                    .collect(),
            },
        )
        .collect();
    let nondet: Vec<NonDetEvent> = nondet
        .into_iter()
        .map(|(seq, source, tag, payload)| NonDetEvent {
            seq,
            source,
            tag,
            payload,
        })
        .collect();
    dump::dump_crash(
        &path,
        meta,
        sources,
        exceptions,
        frames,
        objects,
        nondet,
        recorder(),
    )
    .map_err(|e| PyValueError::new_err(e.to_string()))
}

/// Write a Phase-2 scope recording (`with flight.record()`) to `path`.
///
/// The mutation log is built in Python (the LINE-diff capture) and passed as
/// tuples; this lays down META + MUTATION + SOURCE + EVENT_RING and closes.
#[pyfunction]
#[pyo3(name = "dump_scope")]
#[allow(clippy::too_many_arguments)]
fn dump_scope(
    path: PathBuf,
    python_version: String,
    platform: String,
    argv: Vec<String>,
    cwd: String,
    flight_version: String,
    mutations: Vec<MutationTuple>,
    sources: Vec<SourceTuple>,
) -> PyResult<()> {
    let meta = MetaBlock {
        python_version,
        platform,
        argv,
        cwd,
        flight_version,
    };
    let mutations: Vec<Mutation> = mutations
        .into_iter()
        .map(
            |(seq, kind, name, key, (vkind, vrepr, vtype, vlen), file, qualname, line, frame)| {
                Mutation {
                    seq,
                    kind,
                    name,
                    key,
                    value: MutationValue {
                        kind: vkind,
                        repr: vrepr,
                        type_name: vtype,
                        length: vlen,
                    },
                    file,
                    qualname,
                    line,
                    frame,
                }
            },
        )
        .collect();
    let sources: Vec<SourceFile> = sources
        .into_iter()
        .map(|(filename, sha1, text)| SourceFile {
            filename,
            sha1,
            text,
        })
        .collect();
    dump::dump_scope(&path, meta, mutations, sources, recorder())
        .map_err(|e| PyValueError::new_err(e.to_string()))
}

/// Write a Phase-3 deterministic recording (`with flight.deterministic()`).
#[pyfunction]
#[pyo3(name = "dump_nondet")]
#[allow(clippy::too_many_arguments)]
fn dump_nondet(
    path: PathBuf,
    python_version: String,
    platform: String,
    argv: Vec<String>,
    cwd: String,
    flight_version: String,
    events: Vec<NonDetTuple>,
    sources: Vec<SourceTuple>,
) -> PyResult<()> {
    let meta = MetaBlock {
        python_version,
        platform,
        argv,
        cwd,
        flight_version,
    };
    let events: Vec<NonDetEvent> = events
        .into_iter()
        .map(|(seq, source, tag, payload)| NonDetEvent {
            seq,
            source,
            tag,
            payload,
        })
        .collect();
    let sources: Vec<SourceFile> = sources
        .into_iter()
        .map(|(filename, sha1, text)| SourceFile {
            filename,
            sha1,
            text,
        })
        .collect();
    dump::dump_nondet(&path, meta, events, sources, recorder())
        .map_err(|e| PyValueError::new_err(e.to_string()))
}

/// Read the NONDET tape as a list of `(seq, source, tag, payload)` tuples.
#[pyfunction]
fn read_nondet(py: Python<'_>, path: PathBuf) -> PyResult<Py<PyList>> {
    let f = FlightFile::open(&path).map_err(|e| PyValueError::new_err(e.to_string()))?;
    let out: Vec<NonDetTuple> = f
        .nondet()
        .into_iter()
        .map(|e| (e.seq, e.source, e.tag, e.payload))
        .collect();
    Ok(PyList::new(py, out)?.into())
}

/// Read a `.flight` file and return a summary dict (used by `flight inspect`).
#[pyfunction]
fn read_summary(py: Python<'_>, path: PathBuf) -> PyResult<Py<PyDict>> {
    let f = FlightFile::open(&path).map_err(|e| PyValueError::new_err(e.to_string()))?;
    let d = PyDict::new(py);
    d.set_item("format_version", f.format_version)?;
    d.set_item("flight_version", &f.header.flight_version)?;
    d.set_item("created_unix_ms", f.header.created_unix_ms)?;
    d.set_item("partial", f.partial)?;
    d.set_item("used_index", f.used_index)?;

    let block_names: Vec<&str> = f.blocks.iter().map(|b| b.type_name()).collect();
    d.set_item("blocks", block_names)?;

    let excs: Vec<(String, String, String)> = f
        .exceptions()
        .into_iter()
        .map(|e| (e.exc_type, e.message, e.relation))
        .collect();
    d.set_item("exceptions", excs)?;
    d.set_item("frame_count", f.frames().len())?;
    d.set_item("object_count", f.objects().len())?;
    d.set_item("mutation_count", f.mutations().len())?;
    d.set_item("nondet_count", f.nondet().len())?;

    if let Some(meta) = f.meta() {
        let m = PyDict::new(py);
        m.set_item("python_version", meta.python_version)?;
        m.set_item("platform", meta.platform)?;
        m.set_item("argv", meta.argv)?;
        m.set_item("cwd", meta.cwd)?;
        d.set_item("meta", m)?;
    }

    if let Some(ring) = f.event_ring() {
        d.set_item("event_count", ring.events.len())?;
        d.set_item("wrapped", ring.wrapped)?;
        d.set_item("code_count", ring.codes.len())?;
        // The last few events, resolved to (kind, file, line), for a preview.
        let tail: Vec<(String, String, u32)> = ring
            .events
            .iter()
            .rev()
            .take(10)
            .map(|e| {
                let kind = EventKind::from_u8(e.kind).map(|k| k.name()).unwrap_or("?");
                let file = ring
                    .codes
                    .get(&e.code_id)
                    .map(|c| c.file.clone())
                    .unwrap_or_default();
                (kind.to_string(), file, e.line)
            })
            .collect();
        d.set_item("recent_events", tail)?;
    }

    Ok(d.into())
}

/// Read the full crash detail from a `.flight` file: exception chain, frames
/// with their locals, the object graph, and source texts. Used by the enriched
/// `flight inspect` and, later, the TUI viewer.
#[pyfunction]
fn read_crash(py: Python<'_>, path: PathBuf) -> PyResult<Py<PyDict>> {
    let f = FlightFile::open(&path).map_err(|e| PyValueError::new_err(e.to_string()))?;
    let d = PyDict::new(py);
    d.set_item("partial", f.partial)?;

    let excs: Vec<(String, String, String)> = f
        .exceptions()
        .into_iter()
        .map(|e| (e.exc_type, e.message, e.relation))
        .collect();
    d.set_item("exceptions", excs)?;

    let frames: Vec<FrameTuple> = f
        .frames()
        .into_iter()
        .map(|fr| (fr.file, fr.qualname, fr.lineno, fr.first_lineno, fr.locals))
        .collect();
    d.set_item("frames", frames)?;

    let objects = PyDict::new(py);
    for n in f.objects() {
        let node = PyDict::new(py);
        node.set_item("kind", &n.kind)?;
        node.set_item("repr", &n.repr)?;
        node.set_item("type_name", &n.type_name)?;
        node.set_item("length", n.length)?;
        node.set_item("truncated", n.truncated)?;
        let items: Vec<(Option<String>, u64)> = n
            .items
            .into_iter()
            .map(|it| (it.key, it.value_id))
            .collect();
        node.set_item("items", items)?;
        objects.set_item(n.id, node)?;
    }
    d.set_item("objects", objects)?;

    let sources = PyDict::new(py);
    for s in f.sources() {
        sources.set_item(s.filename, s.text)?;
    }
    d.set_item("sources", sources)?;

    Ok(d.into())
}

/// Read the MUTATION log from a `.flight` file as a list of tuples
/// `(seq, kind, name, key, (vkind, vrepr, vtype, vlen), file, qualname, line, frame)`.
#[pyfunction]
fn read_mutations(py: Python<'_>, path: PathBuf) -> PyResult<Py<PyList>> {
    let f = FlightFile::open(&path).map_err(|e| PyValueError::new_err(e.to_string()))?;
    let out: Vec<MutationTuple> = f
        .mutations()
        .into_iter()
        .map(|m| {
            (
                m.seq,
                m.kind,
                m.name,
                m.key,
                (
                    m.value.kind,
                    m.value.repr,
                    m.value.type_name,
                    m.value.length,
                ),
                m.file,
                m.qualname,
                m.line,
                m.frame,
            )
        })
        .collect();
    Ok(PyList::new(py, out)?.into())
}

/// Read up to `limit` most-recent ring events, resolved to
/// `(kind, file, qualname, line)` in chronological order — the execution path
/// leading up to the end, for the viewer's Events panel.
#[pyfunction]
fn read_events(py: Python<'_>, path: PathBuf, limit: usize) -> PyResult<Py<PyList>> {
    let f = FlightFile::open(&path).map_err(|e| PyValueError::new_err(e.to_string()))?;
    let mut out: Vec<(String, String, String, u32)> = Vec::new();
    if let Some(ring) = f.event_ring() {
        let start = ring.events.len().saturating_sub(limit);
        for e in &ring.events[start..] {
            let kind = EventKind::from_u8(e.kind).map(|k| k.name()).unwrap_or("?");
            let (file, qualname) = ring
                .codes
                .get(&e.code_id)
                .map(|c| (c.file.clone(), c.qualname.clone()))
                .unwrap_or_default();
            out.push((kind.to_string(), file, qualname, e.line));
        }
    }
    Ok(PyList::new(py, out)?.into())
}

/// The Python module `flight._core`.
#[pymodule]
fn _core(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(record, m)?)?;
    m.add_function(wrap_pyfunction!(register_code, m)?)?;
    m.add_function(wrap_pyfunction!(configure, m)?)?;
    m.add_function(wrap_pyfunction!(configure_filter, m)?)?;
    m.add_function(wrap_pyfunction!(cb_line, m)?)?;
    m.add_function(wrap_pyfunction!(cb_py_start, m)?)?;
    m.add_function(wrap_pyfunction!(cb_py_return, m)?)?;
    m.add_function(wrap_pyfunction!(cb_raise, m)?)?;
    m.add_function(wrap_pyfunction!(cb_reraise, m)?)?;
    m.add_function(wrap_pyfunction!(cb_unwind, m)?)?;
    m.add_function(wrap_pyfunction!(stats, m)?)?;
    m.add_function(wrap_pyfunction!(reset, m)?)?;
    m.add_function(wrap_pyfunction!(dump_file, m)?)?;
    m.add_function(wrap_pyfunction!(dump_crash, m)?)?;
    m.add_function(wrap_pyfunction!(dump_scope, m)?)?;
    m.add_function(wrap_pyfunction!(dump_nondet, m)?)?;
    m.add_function(wrap_pyfunction!(read_summary, m)?)?;
    m.add_function(wrap_pyfunction!(read_crash, m)?)?;
    m.add_function(wrap_pyfunction!(read_mutations, m)?)?;
    m.add_function(wrap_pyfunction!(read_events, m)?)?;
    m.add_function(wrap_pyfunction!(read_nondet, m)?)?;

    // Event kind discriminants, so Python names them instead of hard-coding.
    m.add("EVENT_PY_START", EventKind::PyStart as u8)?;
    m.add("EVENT_PY_RETURN", EventKind::PyReturn as u8)?;
    m.add("EVENT_LINE", EventKind::Line as u8)?;
    m.add("EVENT_RAISE", EventKind::Raise as u8)?;
    m.add("EVENT_RERAISE", EventKind::Reraise as u8)?;
    m.add("EVENT_PY_UNWIND", EventKind::PyUnwind as u8)?;

    m.add("__version__", env!("CARGO_PKG_VERSION"))?;
    Ok(())
}
