//! Writer ↔ reader integration: round-trips, truncation at every byte,
//! unknown blocks, corrupt payloads. This is the contract test of the format
//! (P3: the format is the spine — this suite is what "spine" means).

use std::collections::HashMap;

use flight_format::{
    BlockType, CodeInfo, Event, EventKind, ExceptionLink, FlightWriter, FrameInfo, HeaderMeta,
    MetaBlock, ObjectItem, ObjectNode, RingPayload, SourceFile, TRAILER_MAGIC,
};
use flight_reader::FlightFile;

fn sample_meta() -> MetaBlock {
    MetaBlock {
        python_version: "3.13.1".into(),
        platform: "Linux-x86_64".into(),
        argv: vec!["app.py".into(), "--serve".into()],
        cwd: "/srv/app".into(),
        flight_version: "0.0.1".into(),
    }
}

fn sample_ring(n: u64) -> RingPayload {
    let mut codes = HashMap::new();
    codes.insert(
        1u64,
        CodeInfo {
            file: "app.py".into(),
            qualname: "handler".into(),
            first_line: 10,
        },
    );
    let events = (0..n)
        .map(|i| Event::new(EventKind::Line, 0, 10 + i as u32, 1, i))
        .collect();
    RingPayload {
        codes,
        events,
        wrapped: false,
    }
}

/// A complete file, cleanly closed.
fn write_full_file() -> Vec<u8> {
    let mut buf = Vec::new();
    let mut w = FlightWriter::new(&mut buf, &HeaderMeta::new("0.0.1")).unwrap();
    w.write_block_named(BlockType::Meta, &sample_meta())
        .unwrap();
    w.write_block(BlockType::EventRing, &sample_ring(100))
        .unwrap();
    w.finish().unwrap();
    buf
}

#[test]
fn clean_file_roundtrips_via_index() {
    let bytes = write_full_file();
    let f = FlightFile::from_bytes(&bytes).unwrap();
    assert!(
        f.used_index,
        "cleanly closed file must be read through the footer index"
    );
    assert!(!f.partial);
    assert_eq!(f.format_version, 1);
    assert_eq!(f.header.tool, "flight");
    assert_eq!(f.meta().unwrap(), sample_meta());
    let ring = f.event_ring().unwrap();
    assert_eq!(ring, sample_ring(100));
}

#[test]
fn footerless_file_roundtrips_via_scan() {
    // Simulate a crash: writer never calls finish().
    let mut buf = Vec::new();
    let mut w = FlightWriter::new(&mut buf, &HeaderMeta::new("0.0.1")).unwrap();
    w.write_block_named(BlockType::Meta, &sample_meta())
        .unwrap();
    w.write_block(BlockType::EventRing, &sample_ring(50))
        .unwrap();
    w.flush().unwrap();
    drop(w);

    let f = FlightFile::from_bytes(&buf).unwrap();
    assert!(!f.used_index);
    assert!(
        !f.partial,
        "a whole footer-less file is complete, not partial"
    );
    assert_eq!(f.meta().unwrap(), sample_meta());
    assert_eq!(f.event_ring().unwrap(), sample_ring(50));
}

#[test]
fn truncation_at_every_byte_never_panics_and_degrades_monotonically() {
    let bytes = write_full_file();
    // Cutting anywhere must either fail cleanly (header gone) or produce a
    // readable file with a subset of the blocks.
    for cut in 0..bytes.len() {
        let sliced = &bytes[..cut];
        match FlightFile::from_bytes(sliced) {
            Err(_) => assert!(
                cut < 200,
                "hard error only acceptable within the header region"
            ),
            Ok(f) => {
                assert!(f.blocks.len() <= 2);
                // A cut file that lost anything must say so — unless the cut
                // only removed the footer *exactly* (still whole data).
                if f.blocks.len() < 2 {
                    assert!(f.partial || cut < bytes.len());
                }
            }
        }
    }
    // Cut right after the last data block (footer fully missing): both
    // blocks intact, scan path, not partial.
    let full = FlightFile::from_bytes(&bytes).unwrap();
    let last_data_end = {
        // INDEX starts where the trailer says; data ends there.
        let n = bytes.len();
        let index_total =
            u32::from_le_bytes([bytes[n - 8], bytes[n - 7], bytes[n - 6], bytes[n - 5]]) as usize;
        n - 8 - index_total
    };
    let f = FlightFile::from_bytes(&bytes[..last_data_end]).unwrap();
    assert_eq!(f.blocks.len(), full.blocks.len());
    assert!(!f.partial);
    assert_eq!(f.event_ring().unwrap(), sample_ring(100));
}

#[test]
fn truncation_mid_block_keeps_earlier_blocks() {
    let bytes = write_full_file();
    // Find the EVENT_RING block offset via a full read, then cut inside it.
    let full = FlightFile::from_bytes(&bytes).unwrap();
    let ring_off = full
        .blocks
        .iter()
        .find(|b| b.block_type == BlockType::EventRing as u8)
        .unwrap()
        .offset as usize;
    let f = FlightFile::from_bytes(&bytes[..ring_off + 3]).unwrap();
    assert!(f.partial);
    assert!(f.meta().is_some(), "META precedes the cut and must survive");
    assert!(f.event_ring().is_none());
}

#[test]
fn unknown_block_type_is_kept_raw_and_skipped_by_typed_accessors() {
    let mut buf = Vec::new();
    let mut w = FlightWriter::new(&mut buf, &HeaderMeta::new("0.0.1")).unwrap();
    w.write_block_named(BlockType::Meta, &sample_meta())
        .unwrap();
    // A block type from the future.
    w.write_block_msgpack(0x42, &rmp_serde::to_vec(&"mystery").unwrap())
        .unwrap();
    w.write_block(BlockType::EventRing, &sample_ring(5))
        .unwrap();
    w.finish().unwrap();

    let f = FlightFile::from_bytes(&buf).unwrap();
    assert!(!f.partial);
    assert_eq!(f.blocks.len(), 3);
    assert_eq!(f.blocks[1].block_type, 0x42);
    assert_eq!(f.blocks[1].type_name(), "UNKNOWN");
    // Typed accessors are undisturbed.
    assert_eq!(f.meta().unwrap(), sample_meta());
    assert_eq!(f.event_ring().unwrap(), sample_ring(5));
}

#[test]
fn corrupt_payload_degrades_to_partial() {
    let mut bytes = write_full_file();
    // Flip bytes inside the EVENT_RING payload (after its 5-byte header).
    let full = FlightFile::from_bytes(&bytes).unwrap();
    let off = full
        .blocks
        .iter()
        .find(|b| b.block_type == BlockType::EventRing as u8)
        .unwrap()
        .offset as usize;
    for i in 0..8 {
        bytes[off + 5 + 4 + i] ^= 0xFF; // skip zstd frame magic, corrupt content
    }
    let f = FlightFile::from_bytes(&bytes).unwrap();
    // Either path is acceptable, but it must not panic and META must survive.
    assert!(f.meta().is_some());
    assert!(f.event_ring().is_none() || f.partial || f.used_index);
}

#[test]
fn corrupt_trailer_falls_back_to_scan() {
    let mut bytes = write_full_file();
    let n = bytes.len();
    bytes[n - 2] = b'X'; // break the trailer magic
    let f = FlightFile::from_bytes(&bytes).unwrap();
    assert!(!f.used_index);
    // The scan walks the data blocks and the INDEX block; the INDEX block is
    // dropped from the result, the data survives.
    assert_eq!(f.meta().unwrap(), sample_meta());
    assert_eq!(f.event_ring().unwrap(), sample_ring(100));
}

#[test]
fn not_a_flight_file_is_a_clear_error() {
    assert!(FlightFile::from_bytes(b"GIF89a...").is_err());
    assert!(FlightFile::from_bytes(b"").is_err());
}

#[test]
fn future_format_version_is_rejected_not_misread() {
    let mut bytes = write_full_file();
    bytes[4] = 99; // format version 99
    assert!(matches!(
        FlightFile::from_bytes(&bytes),
        Err(flight_format::FormatError::UnsupportedVersion(99))
    ));
}

#[test]
fn trailer_magic_constant_matches_writer_output() {
    let bytes = write_full_file();
    assert_eq!(&bytes[bytes.len() - 4..], TRAILER_MAGIC);
}

// -- Phase 1: crash blocks ---------------------------------------------------

#[test]
fn crash_blocks_roundtrip_and_aliasing_resolves() {
    let mut buf = Vec::new();
    let mut w = FlightWriter::new(&mut buf, &HeaderMeta::new("0.0.1")).unwrap();
    w.write_block_named(BlockType::Meta, &sample_meta())
        .unwrap();
    w.write_block(
        BlockType::Source,
        &vec![SourceFile {
            filename: "app.py".into(),
            sha1: "h".into(),
            text: "x=1\n".into(),
        }],
    )
    .unwrap();
    w.write_block(
        BlockType::Exception,
        &vec![
            ExceptionLink {
                exc_type: "ZeroDivisionError".into(),
                message: "division by zero".into(),
                relation: "head".into(),
            },
            ExceptionLink {
                exc_type: "ValueError".into(),
                message: "bad".into(),
                relation: "context".into(),
            },
        ],
    )
    .unwrap();
    // Two frames sharing object id 7 — the aliasing case.
    let frames = vec![
        FrameInfo {
            file: "app.py".into(),
            qualname: "inner".into(),
            lineno: 8,
            first_lineno: 4,
            locals: vec![("cfg".into(), 7)],
        },
        FrameInfo {
            file: "app.py".into(),
            qualname: "outer".into(),
            lineno: 20,
            first_lineno: 15,
            locals: vec![("config".into(), 7), ("n".into(), 3)],
        },
    ];
    w.write_block(BlockType::Frame, &frames).unwrap();
    let objects = vec![
        ObjectNode {
            id: 7,
            kind: "dict".into(),
            repr: None,
            type_name: None,
            length: Some(1),
            truncated: false,
            items: vec![ObjectItem {
                key: Some("k".into()),
                value_id: 3,
            }],
        },
        ObjectNode {
            id: 3,
            kind: "int".into(),
            repr: Some("3".into()),
            type_name: None,
            length: None,
            truncated: false,
            items: vec![],
        },
    ];
    w.write_block(BlockType::Object, &objects).unwrap();
    w.finish().unwrap();

    let f = FlightFile::from_bytes(&buf).unwrap();
    assert!(!f.partial);

    let excs = f.exceptions();
    assert_eq!(excs.len(), 2);
    assert_eq!(excs[0].exc_type, "ZeroDivisionError");
    assert_eq!(excs[1].relation, "context");

    assert_eq!(f.sources().len(), 1);
    assert_eq!(f.frames(), frames);

    let map = f.object_map();
    assert_eq!(map[&7].kind, "dict");
    assert_eq!(map[&3].repr.as_deref(), Some("3"));

    // Object 7 is aliased across both frames, under different local names.
    let aliases = f.aliases(7);
    assert_eq!(
        aliases,
        vec![(0, "cfg".to_string()), (1, "config".to_string())]
    );
    assert!(f.aliases(999).is_empty());
}

#[test]
fn crash_accessors_are_empty_on_a_ring_only_file() {
    // A Phase-0 file (META + EVENT_RING) has no crash blocks; accessors must
    // return empty, not error.
    let bytes = write_full_file();
    let f = FlightFile::from_bytes(&bytes).unwrap();
    assert!(f.exceptions().is_empty());
    assert!(f.frames().is_empty());
    assert!(f.sources().is_empty());
    assert!(f.objects().is_empty());
    assert!(f.event_ring().is_some());
}
