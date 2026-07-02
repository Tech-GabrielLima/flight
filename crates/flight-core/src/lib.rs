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

use std::panic::catch_unwind;
use std::path::PathBuf;
use std::sync::atomic::{AtomicU64, AtomicUsize, Ordering};
use std::sync::OnceLock;

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::PyDict;

use flight_format::{
    EventKind, ExceptionLink, FrameInfo, MetaBlock, ObjectItem, ObjectNode, SourceFile,
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

/// The process-global recorder, built once on first use.
static RECORDER: OnceLock<Recorder> = OnceLock::new();
/// Ring capacity to use when the recorder is first built. Settable via
/// [`configure`] before the recorder exists.
static RING_CAP: AtomicUsize = AtomicUsize::new(4096);

fn recorder() -> &'static Recorder {
    RECORDER.get_or_init(|| Recorder::new(RING_CAP.load(Ordering::Relaxed)))
}

/// A unique, stable id per OS thread, assigned lazily. Cheaper than calling
/// into Python's `threading.get_ident()` on every event.
fn thread_id() -> u64 {
    thread_local! {
        static TID: u64 = {
            static NEXT: AtomicU64 = AtomicU64::new(1);
            NEXT.fetch_add(1, Ordering::Relaxed)
        };
    }
    TID.with(|&t| t)
}

/// Record one execution event. Hot path — must stay allocation-free and must
/// never raise into the interpreter.
///
/// `kind` is an [`EventKind`] discriminant (see the module constants exported
/// to Python). Unknown kinds are ignored.
#[pyfunction]
fn record(kind: u8, code_id: u64, line: u32) {
    let _ = catch_unwind(|| {
        if let Some(k) = EventKind::from_u8(kind) {
            recorder().record(k, thread_id(), code_id, line);
        }
    });
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
    dump::dump_crash(
        &path,
        meta,
        sources,
        exceptions,
        frames,
        objects,
        recorder(),
    )
    .map_err(|e| PyValueError::new_err(e.to_string()))
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

/// The Python module `flight._core`.
#[pymodule]
fn _core(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(record, m)?)?;
    m.add_function(wrap_pyfunction!(register_code, m)?)?;
    m.add_function(wrap_pyfunction!(configure, m)?)?;
    m.add_function(wrap_pyfunction!(stats, m)?)?;
    m.add_function(wrap_pyfunction!(reset, m)?)?;
    m.add_function(wrap_pyfunction!(dump_file, m)?)?;
    m.add_function(wrap_pyfunction!(dump_crash, m)?)?;
    m.add_function(wrap_pyfunction!(read_summary, m)?)?;
    m.add_function(wrap_pyfunction!(read_crash, m)?)?;

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
