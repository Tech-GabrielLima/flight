use flight_format::BlockType;
use flight_reader::FlightFile;
use serde_json::{json, Value};
use std::collections::HashMap;


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

    let omap = f.object_map();
    let raw_frames = f.frames();
    let mut alias_ct: HashMap<u64, usize> = HashMap::new();
    for fr in &raw_frames {
        for (_, id) in &fr.locals {
            *alias_ct.entry(*id).or_insert(0) += 1;
        }
    }
    let srcs = f.sources();
    let source_text = |file: &str| -> Option<&String> {
        srcs.iter()
            .find(|s| s.filename == file)
            .or_else(|| {
                let b = file.rsplit('/').next().unwrap_or(file);
                srcs.iter().find(|s| s.filename.rsplit('/').next().unwrap_or("") == b)
            })
            .map(|s| &s.text)
    };

    let frames: Vec<Value> = raw_frames
        .iter()
        .map(|fr| {
            let locals: Vec<Value> = fr
                .locals
                .iter()
                .map(|(name, id)| {
                    let (val, ty) = match omap.get(id) {
                        Some(n) => {
                            let ty = n.type_name.clone().unwrap_or_else(|| n.kind.clone());
                            let val = match &n.repr {
                                Some(r) if !r.is_empty() => r.clone(),
                                _ => match n.length {
                                    Some(l) => format!("{}[{}]", n.kind, l),
                                    None => n.kind.clone(),
                                },
                            };
                            (val, ty)
                        }
                        None => ("<unavailable>".to_string(), "?".to_string()),
                    };
                    json!({
                        "name": name, "value": val, "type": ty,
                        "aliased": alias_ct.get(id).copied().unwrap_or(0) > 1,
                    })
                })
                .collect();

            let mut window: Vec<Value> = Vec::new();
            if let Some(text) = source_text(&fr.file) {
                let lines: Vec<&str> = text.split('\n').collect();
                let ln = fr.lineno as usize;
                if ln >= 1 && ln <= lines.len() {
                    let start = ln.saturating_sub(4).max(1);
                    let end = (ln + 3).min(lines.len());
                    for n in start..=end {
                        window.push(json!({ "n": n, "text": lines[n - 1], "crash": n == ln }));
                    }
                }
            }

            json!({
                "qualname": fr.qualname, "file": fr.file,
                "lineno": fr.lineno, "first_lineno": fr.first_lineno,
                "locals": locals,
                "source": window,
            })
        })
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
