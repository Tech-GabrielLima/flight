use flight_format::{compress, decompress, from_msgpack, to_msgpack, CodeInfo, Event, EventKind, IndexEntry};

fn rt<T>(v: &T) -> T
where
    T: serde::Serialize + serde::de::DeserializeOwned,
{
    from_msgpack(&to_msgpack(v).unwrap()).unwrap()
}


fn mp_str(s: &str) -> Vec<u8> {
    let b = s.as_bytes();
    assert!(b.len() < 32);
    let mut v = vec![0xA0 | b.len() as u8];
    v.extend_from_slice(b);
    v
}


#[test]
fn event_rt_basic() {
    let e = Event::new(EventKind::Line, 3, 87, 0xDEAD_BEEF, 42);
    assert_eq!(rt(&e), e);
}

#[test]
fn event_rt_all_kinds() {
    for k in [
        EventKind::PyStart,
        EventKind::PyReturn,
        EventKind::Line,
        EventKind::Raise,
        EventKind::Reraise,
        EventKind::PyUnwind,
    ] {
        let e = Event::new(k, 1, 2, 3, 4);
        let back = rt(&e);
        assert_eq!(back, e);
        assert_eq!(back.kind(), Some(k));
    }
}

#[test]
fn event_rt_zero_fields() {
    let e = Event {
        kind: 1,
        thread: 0,
        line: 0,
        code_id: 0,
        tstamp: 0,
    };
    assert_eq!(rt(&e), e);
}

#[test]
fn event_rt_max_fields() {
    let e = Event {
        kind: 6,
        thread: u16::MAX,
        line: u32::MAX,
        code_id: u64::MAX,
        tstamp: u64::MAX,
    };
    let back = rt(&e);
    assert_eq!(back, e);
    assert_eq!(back.thread, u16::MAX);
    assert_eq!(back.line, u32::MAX);
    assert_eq!(back.code_id, u64::MAX);
    assert_eq!(back.tstamp, u64::MAX);
}

#[test]
fn event_rt_preserves_raw_kind_byte() {

    let e = Event {
        kind: 42,
        thread: 7,
        line: 9,
        code_id: 11,
        tstamp: 13,
    };
    assert_eq!(rt(&e).kind, 42);
}

#[test]
fn event_encodes_as_fixarray_of_5() {


    let e = Event::new(EventKind::Line, 0, 0, 0, 0);
    let bytes = to_msgpack(&e).unwrap();
    assert_eq!(bytes[0], 0x95);
}

#[test]
fn event_rt_many_tstamps() {
    for t in 0u64..500 {
        let e = Event::new(EventKind::Line, (t % 65535) as u16, t as u32, t * 7, t);
        assert_eq!(rt(&e), e);
    }
}


#[test]
fn codeinfo_rt_basic() {
    let c = CodeInfo {
        file: "app.py".into(),
        qualname: "main".into(),
        first_line: 1,
    };
    assert_eq!(rt(&c), c);
}

#[test]
fn codeinfo_rt_empty_strings() {
    let c = CodeInfo {
        file: String::new(),
        qualname: String::new(),
        first_line: 0,
    };
    assert_eq!(rt(&c), c);
}

#[test]
fn codeinfo_rt_unicode() {
    let c = CodeInfo {
        file: "café/módulo_日本語.py".into(),
        qualname: "Класс.método".into(),
        first_line: 12345,
    };
    assert_eq!(rt(&c), c);
}

#[test]
fn codeinfo_rt_max_first_line() {
    let c = CodeInfo {
        file: "x".into(),
        qualname: "y".into(),
        first_line: u32::MAX,
    };
    assert_eq!(rt(&c), c);
}

#[test]
fn codeinfo_encodes_as_fixarray_of_3() {
    let c = CodeInfo {
        file: "a".into(),
        qualname: "b".into(),
        first_line: 1,
    };
    let bytes = to_msgpack(&c).unwrap();
    assert_eq!(bytes[0], 0x93);
}

#[test]
fn codeinfo_tolerates_extra_named_field() {


    let mut bytes = vec![0x84];
    bytes.extend(mp_str("file"));
    bytes.extend(mp_str("a.py"));
    bytes.extend(mp_str("qualname"));
    bytes.extend(mp_str("run"));
    bytes.extend(mp_str("first_line"));
    bytes.push(0x07);
    bytes.extend(mp_str("some_future_field"));
    bytes.push(0xC3);
    let c: CodeInfo = from_msgpack(&bytes).unwrap();
    assert_eq!(c.file, "a.py");
    assert_eq!(c.qualname, "run");
    assert_eq!(c.first_line, 7);
}

#[test]
fn codeinfo_decodes_from_named_map_form() {
    let mut bytes = vec![0x83];
    bytes.extend(mp_str("file"));
    bytes.extend(mp_str("m.py"));
    bytes.extend(mp_str("qualname"));
    bytes.extend(mp_str("f"));
    bytes.extend(mp_str("first_line"));
    bytes.push(0x2A);
    let c: CodeInfo = from_msgpack(&bytes).unwrap();
    assert_eq!(c.first_line, 42);
    assert_eq!(c.file, "m.py");
}


#[test]
fn indexentry_rt_basic() {
    let e = IndexEntry {
        block_type: 0x06,
        offset: 128,
        payload_len: 4096,
    };
    assert_eq!(rt(&e), e);
}

#[test]
fn indexentry_rt_max_values() {
    let e = IndexEntry {
        block_type: 0xFF,
        offset: u64::MAX,
        payload_len: u32::MAX,
    };
    let back = rt(&e);
    assert_eq!(back, e);
    assert_eq!(back.offset, u64::MAX);
    assert_eq!(back.payload_len, u32::MAX);
}

#[test]
fn indexentry_rt_zero() {
    let e = IndexEntry {
        block_type: 0,
        offset: 0,
        payload_len: 0,
    };
    assert_eq!(rt(&e), e);
}

#[test]
fn indexentry_vec_rt() {
    let v = vec![
        IndexEntry {
            block_type: 0x01,
            offset: 10,
            payload_len: 20,
        },
        IndexEntry {
            block_type: 0x06,
            offset: 35,
            payload_len: 999,
        },
        IndexEntry {
            block_type: 0x70,
            offset: 1039,
            payload_len: 12,
        },
    ];
    assert_eq!(rt(&v), v);
}

#[test]
fn indexentry_empty_vec_rt() {
    let v: Vec<IndexEntry> = Vec::new();
    assert_eq!(rt(&v), v);

    assert_eq!(to_msgpack(&v).unwrap(), vec![0x90]);
}

#[test]
fn indexentry_encodes_as_fixarray_of_3() {
    let e = IndexEntry {
        block_type: 1,
        offset: 2,
        payload_len: 3,
    };
    assert_eq!(to_msgpack(&e).unwrap()[0], 0x93);
}

#[test]
fn indexentry_large_vec_rt() {
    let v: Vec<IndexEntry> = (0..1000)
        .map(|i| IndexEntry {
            block_type: (i % 256) as u8,
            offset: i as u64 * 1024,
            payload_len: (i * 3) as u32,
        })
        .collect();
    assert_eq!(rt(&v), v);
}


#[test]
fn compress_decompress_empty() {
    let c = compress(b"").unwrap();
    assert_eq!(decompress(&c).unwrap(), b"");
}

#[test]
fn compress_decompress_small() {
    let data = b"hello, flight recorder";
    let c = compress(data).unwrap();
    assert_eq!(decompress(&c).unwrap(), data);
}

#[test]
fn compress_decompress_single_byte() {
    let c = compress(&[0x42]).unwrap();
    assert_eq!(decompress(&c).unwrap(), vec![0x42]);
}

#[test]
fn compress_decompress_64k_plus() {
    let data: Vec<u8> = (0..70_000u32).map(|i| (i % 251) as u8).collect();
    let c = compress(&data).unwrap();
    assert_eq!(decompress(&c).unwrap(), data);
}

#[test]
fn compress_decompress_over_1mib() {
    let data: Vec<u8> = (0..1_200_000u32).map(|i| (i.wrapping_mul(2654435761) >> 24) as u8).collect();
    let c = compress(&data).unwrap();
    assert_eq!(decompress(&c).unwrap(), data);
}

#[test]
fn compress_highly_compressible_shrinks() {
    let data = vec![0u8; 100_000];
    let c = compress(&data).unwrap();
    assert!(c.len() < data.len() / 10, "zeros should compress hugely: {}", c.len());
    assert_eq!(decompress(&c).unwrap(), data);
}

#[test]
fn compress_repeated_pattern_shrinks() {
    let unit = b"the quick brown fox ";
    let mut data = Vec::new();
    for _ in 0..5000 {
        data.extend_from_slice(unit);
    }
    let c = compress(&data).unwrap();
    assert!(c.len() < data.len() / 5);
    assert_eq!(decompress(&c).unwrap(), data);
}

#[test]
fn compress_randomish_roundtrips() {

    let mut state = 0x1234_5678_9abc_def0u64;
    let data: Vec<u8> = (0..50_000)
        .map(|_| {
            state ^= state << 13;
            state ^= state >> 7;
            state ^= state << 17;
            (state & 0xFF) as u8
        })
        .collect();
    let c = compress(&data).unwrap();
    assert_eq!(decompress(&c).unwrap(), data);
}

#[test]
fn compress_all_byte_values() {
    let data: Vec<u8> = (0..=255u8).collect();
    let c = compress(&data).unwrap();
    assert_eq!(decompress(&c).unwrap(), data);
}

#[test]
fn compress_is_deterministic() {
    let data = b"determinism matters for reproducible artifacts";
    assert_eq!(compress(data).unwrap(), compress(data).unwrap());
}

#[test]
fn compress_deterministic_large() {
    let data: Vec<u8> = (0..80_000u32).map(|i| (i % 97) as u8).collect();
    assert_eq!(compress(&data).unwrap(), compress(&data).unwrap());
}

#[test]
fn decompress_rejects_garbage() {
    assert!(decompress(b"this is definitely not a zstd frame").is_err());
}

#[test]
fn decompress_rejects_all_ff() {
    assert!(decompress(&[0xFFu8; 32]).is_err());
}

#[test]
fn decompress_rejects_truncated_frame() {
    let c = compress(b"some reasonably long payload to compress into a frame").unwrap();

    let truncated = &c[..c.len() / 2];
    match decompress(truncated) {
        Ok(out) => assert_ne!(out.as_slice(), b"some reasonably long payload to compress into a frame"),
        Err(_) => {}
    }
}

#[test]
fn compress_then_manual_decompress_roundtrip() {

    let c = compress(b"x").unwrap();
    assert!(!c.is_empty());
    assert_eq!(decompress(&c).unwrap(), b"x");
}

#[test]
fn compress_roundtrip_binary_with_nuls() {
    let data = vec![0u8, 1, 2, 0, 0, 255, 0, 128, 0];
    let c = compress(&data).unwrap();
    assert_eq!(decompress(&c).unwrap(), data);
}

#[test]
fn compress_roundtrip_msgpack_of_events() {

    let events: Vec<Event> = (0..2000)
        .map(|i| Event::new(EventKind::Line, 0, i, 1, i as u64))
        .collect();
    let msg = to_msgpack(&events).unwrap();
    let c = compress(&msg).unwrap();
    let back: Vec<Event> = from_msgpack(&decompress(&c).unwrap()).unwrap();
    assert_eq!(back, events);
}
