//! The `.flight` reader, compiled to WebAssembly (Phase 9).
//!
//! This is the growth loop from the VISION: a `.flight` is shareable, so make it
//! openable with *no install* — drag it onto a web page and read the black box
//! in the browser. The parsing is the real Rust `flight-reader` (with the
//! pure-Rust `ruzstd` decoder so it targets `wasm32`), exposed through a tiny
//! raw C ABI so the page needs nothing but the standard `WebAssembly` API — no
//! wasm-bindgen, no bundler, no runtime.
//!
//! ABI: JS calls [`alloc`] to get a buffer, copies the file bytes in, calls
//! [`parse`] which returns a pointer to `[u32 little-endian json_len][json…]`,
//! reads that UTF-8 JSON, then frees both buffers with [`dealloc`]/[`free`].

use flight_format::BlockType;
use flight_reader::FlightFile;
use serde_json::{json, Value};

/// Allocate `len` bytes in the wasm linear memory and return the pointer.
#[no_mangle]
pub extern "C" fn alloc(len: usize) -> *mut u8 {
    let mut buf = vec![0u8; len];
    let ptr = buf.as_mut_ptr();
    std::mem::forget(buf);
    ptr
}

/// Free a buffer previously handed out by [`alloc`] (same `len`).
///
/// # Safety
/// `ptr`/`len` must come from a prior [`alloc`] call.
#[no_mangle]
pub unsafe extern "C" fn dealloc(ptr: *mut u8, len: usize) {
    if !ptr.is_null() && len != 0 {
        let _ = Vec::from_raw_parts(ptr, len, len);
    }
}

/// Free a result buffer returned by [`parse`] (its total length is `4 + json`).
///
/// # Safety
/// `ptr`/`len` must be exactly what [`parse`] returned.
#[no_mangle]
pub unsafe extern "C" fn free(ptr: *mut u8, len: usize) {
    dealloc(ptr, len)
}

/// Parse the `.flight` bytes at `ptr[..len]` and return a pointer to a length-
/// prefixed JSON summary: 4 bytes little-endian length, then that many UTF-8
/// bytes. Never traps — a parse error becomes an `{ "error": … }` document.
///
/// # Safety
/// `ptr`/`len` must describe a buffer from [`alloc`] holding the file bytes.
#[no_mangle]
pub unsafe extern "C" fn parse(ptr: *const u8, len: usize) -> *mut u8 {
    let bytes = if ptr.is_null() { &[][..] } else { std::slice::from_raw_parts(ptr, len) };
    let json = match FlightFile::from_bytes(bytes) {
        Ok(f) => summary(&f),
        Err(e) => json!({ "error": e.to_string() }),
    };
    let jb = serde_json::to_vec(&json).unwrap_or_else(|_| b"{\"error\":\"encode\"}".to_vec());
    let total = 4 + jb.len();
    let mut out = vec![0u8; total];
    out[0..4].copy_from_slice(&(jb.len() as u32).to_le_bytes());
    out[4..].copy_from_slice(&jb);
    let p = out.as_mut_ptr();
    std::mem::forget(out);
    p
}

fn summary(f: &FlightFile) -> Value {
    let block_names: Vec<&str> = f
        .blocks
        .iter()
        .map(|b| BlockType::from_u8(b.block_type).map(|t| t.name()).unwrap_or("UNKNOWN"))
        .collect();

    let meta = f.meta().map(|m| {
        json!({
            "python_version": m.python_version,
            "platform": m.platform,
            "argv": m.argv,
            "cwd": m.cwd,
            "flight_version": m.flight_version,
        })
    });

    let (event_count, code_count, wrapped, recent) = match f.event_ring() {
        Some(ring) => {
            let mut evs: Vec<Value> = Vec::new();
            for e in ring.events.iter().rev().take(200) {
                let (file, qualname) = ring
                    .codes
                    .get(&e.code_id)
                    .map(|c| (c.file.clone(), c.qualname.clone()))
                    .unwrap_or_default();
                let kind = flight_format::EventKind::from_u8(e.kind)
                    .map(|k| k.name())
                    .unwrap_or("?");
                evs.push(json!({
                    "kind": kind, "file": file, "qualname": qualname, "line": e.line,
                }));
            }
            (ring.events.len(), ring.codes.len(), ring.wrapped, evs)
        }
        None => (0usize, 0usize, false, Vec::new()),
    };

    let exceptions: Vec<Value> = f
        .exceptions()
        .into_iter()
        .map(|e| json!({ "type": e.exc_type, "message": e.message, "relation": e.relation }))
        .collect();

    let frames: Vec<Value> = f
        .frames()
        .into_iter()
        .map(|fr| json!({
            "qualname": fr.qualname, "file": fr.file,
            "lineno": fr.lineno, "first_lineno": fr.first_lineno,
            "locals": fr.locals.len(),
        }))
        .collect();

    json!({
        "format_version": f.format_version,
        "flight_version": f.header.flight_version,
        "created_unix_ms": f.header.created_unix_ms,
        "partial": f.partial,
        "used_index": f.used_index,
        "blocks": block_names,
        "meta": meta,
        "event_count": event_count,
        "code_count": code_count,
        "wrapped": wrapped,
        "recent_events": recent,
        "exceptions": exceptions,
        "frames": frames,
    })
}
