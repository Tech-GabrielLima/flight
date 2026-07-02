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

use flight_format::{EventKind, MetaBlock};
use flight_reader::FlightFile;
use recorder::Recorder;

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

/// The Python module `flight._core`.
#[pymodule]
fn _core(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(record, m)?)?;
    m.add_function(wrap_pyfunction!(register_code, m)?)?;
    m.add_function(wrap_pyfunction!(configure, m)?)?;
    m.add_function(wrap_pyfunction!(stats, m)?)?;
    m.add_function(wrap_pyfunction!(reset, m)?)?;
    m.add_function(wrap_pyfunction!(dump_file, m)?)?;
    m.add_function(wrap_pyfunction!(read_summary, m)?)?;

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
