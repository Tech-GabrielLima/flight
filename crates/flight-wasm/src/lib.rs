use flight_format::BlockType;
use flight_reader::FlightFile;
use serde_json::{json, Value};


#[no_mangle]
pub extern "C" fn alloc(len: usize) -> *mut u8 {
    let mut buf = vec![0u8; len];
    let ptr = buf.as_mut_ptr();
    std::mem::forget(buf);
    ptr
}


#[no_mangle]
pub unsafe extern "C" fn dealloc(ptr: *mut u8, len: usize) {
    if !ptr.is_null() && len != 0 {
        let _ = Vec::from_raw_parts(ptr, len, len);
    }
}


#[no_mangle]
pub unsafe extern "C" fn free(ptr: *mut u8, len: usize) {
    dealloc(ptr, len)
}


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
