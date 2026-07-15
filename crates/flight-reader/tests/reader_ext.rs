use std::collections::HashMap;

use flight_format::{
    BlockType, CodeInfo, Event, EventKind, ExceptionLink, FlightWriter, FrameInfo, HeaderMeta,
    MetaBlock, Mutation, MutationValue, NonDetEvent, ObjectItem, ObjectNode, RingPayload,
    SourceFile, HEADER_FIXED_LEN, TRAILER_LEN, TRAILER_MAGIC,
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

fn sample_frames() -> Vec<FrameInfo> {
    vec![
        FrameInfo {
            file: "app.py".into(),
            qualname: "inner".into(),
            lineno: 8,
            first_lineno: 4,
            locals: vec![("cfg".into(), 7), ("x".into(), 3)],
        },
        FrameInfo {
            file: "app.py".into(),
            qualname: "outer".into(),
            lineno: 20,
            first_lineno: 15,
            locals: vec![("config".into(), 7), ("n".into(), 3)],
        },
    ]
}

fn sample_objects() -> Vec<ObjectNode> {
    vec![
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
    ]
}

fn sample_exceptions() -> Vec<ExceptionLink> {
    vec![
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
    ]
}

fn sample_mutations() -> Vec<Mutation> {
    vec![
        Mutation {
            seq: 0,
            kind: "local".into(),
            name: "total".into(),
            key: None,
            value: MutationValue {
                kind: "int".into(),
                repr: Some("0".into()),
                type_name: None,
                length: None,
            },
            file: "app.py".into(),
            qualname: "run".into(),
            line: 5,
            frame: 1,
        },
        Mutation {
            seq: 3,
            kind: "local".into(),
            name: "total".into(),
            key: None,
            value: MutationValue {
                kind: "int".into(),
                repr: Some("45".into()),
                type_name: None,
                length: None,
            },
            file: "app.py".into(),
            qualname: "run".into(),
            line: 5,
            frame: 1,
        },
    ]
}

fn sample_nondet() -> Vec<NonDetEvent> {
    vec![
        NonDetEvent {
            seq: 0,
            source: "time.time".into(),
            tag: "f".into(),
            payload: "1783000000.5".into(),
        },
        NonDetEvent {
            seq: 1,
            source: "random.random".into(),
            tag: "f".into(),
            payload: "0.375".into(),
        },
    ]
}

fn sample_source(name: &str) -> SourceFile {
    SourceFile {
        filename: name.into(),
        sha1: format!("sha-{name}"),
        text: format!("# {name}\nx = 1\n"),
    }
}

fn write_kitchen_sink() -> Vec<u8> {
    let mut buf = Vec::new();
    let mut w = FlightWriter::new(&mut buf, &HeaderMeta::new("0.0.1")).unwrap();
    w.write_block_named(BlockType::Meta, &sample_meta())
        .unwrap();
    w.write_block(BlockType::Exception, &sample_exceptions())
        .unwrap();
    w.write_block(BlockType::Frame, &sample_frames()).unwrap();
    w.write_block(BlockType::Object, &sample_objects()).unwrap();
    w.write_block(BlockType::Mutation, &sample_mutations())
        .unwrap();
    w.write_block(BlockType::Nondet, &sample_nondet()).unwrap();
    w.write_block(BlockType::Source, &vec![sample_source("a.py")])
        .unwrap();
    w.write_block(BlockType::Source, &vec![sample_source("b.py")])
        .unwrap();

    w.write_block_msgpack(
        BlockType::Timeline as u8,
        &rmp_serde::to_vec(&"tl").unwrap(),
    )
    .unwrap();

    w.write_block_msgpack(BlockType::Ext as u8, &rmp_serde::to_vec(&"ext").unwrap())
        .unwrap();

    w.write_block_msgpack(0x42, &rmp_serde::to_vec(&"mystery").unwrap())
        .unwrap();
    w.write_block(BlockType::EventRing, &sample_ring(30))
        .unwrap();
    w.finish().unwrap();
    buf
}

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
fn format_version_is_one() {
    let f = FlightFile::from_bytes(&write_full_file()).unwrap();
    assert_eq!(f.format_version, 1);
}

#[test]
fn header_tool_is_flight() {
    let f = FlightFile::from_bytes(&write_full_file()).unwrap();
    assert_eq!(f.header.tool, "flight");
}

#[test]
fn header_flight_version_roundtrips() {
    let mut buf = Vec::new();
    let w = FlightWriter::new(&mut buf, &HeaderMeta::new("9.9.9")).unwrap();
    drop(w);
    let f = FlightFile::from_bytes(&buf).unwrap();
    assert_eq!(f.header.flight_version, "9.9.9");
}

#[test]
fn header_created_ms_preserved() {
    let mut meta = HeaderMeta::new("0.0.1");
    meta.created_unix_ms = 1_783_000_000_000;
    let mut buf = Vec::new();
    let w = FlightWriter::new(&mut buf, &meta).unwrap();
    drop(w);
    let f = FlightFile::from_bytes(&buf).unwrap();
    assert_eq!(f.header.created_unix_ms, 1_783_000_000_000);
}

#[test]
fn meta_accessor_decodes_all_fields() {
    let f = FlightFile::from_bytes(&write_kitchen_sink()).unwrap();
    assert_eq!(f.meta().unwrap(), sample_meta());
}

#[test]
fn event_ring_accessor_decodes() {
    let f = FlightFile::from_bytes(&write_kitchen_sink()).unwrap();
    assert_eq!(f.event_ring().unwrap(), sample_ring(30));
}

#[test]
fn event_ring_wrapped_flag_roundtrips() {
    let mut ring = sample_ring(5);
    ring.wrapped = true;
    let mut buf = Vec::new();
    let mut w = FlightWriter::new(&mut buf, &HeaderMeta::new("0.0.1")).unwrap();
    w.write_block(BlockType::EventRing, &ring).unwrap();
    w.finish().unwrap();
    let f = FlightFile::from_bytes(&buf).unwrap();
    assert!(f.event_ring().unwrap().wrapped);
}

#[test]
fn empty_ring_roundtrips() {
    let f = {
        let mut buf = Vec::new();
        let mut w = FlightWriter::new(&mut buf, &HeaderMeta::new("0.0.1")).unwrap();
        w.write_block(BlockType::EventRing, &sample_ring(0))
            .unwrap();
        w.finish().unwrap();
        FlightFile::from_bytes(&buf).unwrap()
    };
    assert_eq!(f.event_ring().unwrap().events.len(), 0);
}

#[test]
fn large_ring_roundtrips() {
    let mut buf = Vec::new();
    let mut w = FlightWriter::new(&mut buf, &HeaderMeta::new("0.0.1")).unwrap();
    w.write_block(BlockType::EventRing, &sample_ring(10_000))
        .unwrap();
    w.finish().unwrap();
    let f = FlightFile::from_bytes(&buf).unwrap();
    assert_eq!(f.event_ring().unwrap().events.len(), 10_000);
}

#[test]
fn exceptions_accessor_order_is_head_first() {
    let f = FlightFile::from_bytes(&write_kitchen_sink()).unwrap();
    let excs = f.exceptions();
    assert_eq!(excs.len(), 2);
    assert_eq!(excs[0].exc_type, "ZeroDivisionError");
    assert_eq!(excs[0].relation, "head");
}

#[test]
fn exceptions_relation_chain() {
    let f = FlightFile::from_bytes(&write_kitchen_sink()).unwrap();
    assert_eq!(f.exceptions()[1].relation, "context");
}

#[test]
fn frames_accessor_full_roundtrip() {
    let f = FlightFile::from_bytes(&write_kitchen_sink()).unwrap();
    assert_eq!(f.frames(), sample_frames());
}

#[test]
fn frames_locals_preserved() {
    let f = FlightFile::from_bytes(&write_kitchen_sink()).unwrap();
    let frames = f.frames();
    assert_eq!(
        frames[0].locals,
        vec![("cfg".to_string(), 7), ("x".to_string(), 3)]
    );
}

#[test]
fn frames_line_numbers_preserved() {
    let f = FlightFile::from_bytes(&write_kitchen_sink()).unwrap();
    let frames = f.frames();
    assert_eq!(frames[1].lineno, 20);
    assert_eq!(frames[1].first_lineno, 15);
}

#[test]
fn objects_accessor_roundtrip() {
    let f = FlightFile::from_bytes(&write_kitchen_sink()).unwrap();
    assert_eq!(f.objects(), sample_objects());
}

#[test]
fn object_map_indexes_by_id() {
    let f = FlightFile::from_bytes(&write_kitchen_sink()).unwrap();
    let map = f.object_map();
    assert_eq!(map[&7].kind, "dict");
    assert_eq!(map[&3].repr.as_deref(), Some("3"));
}

#[test]
fn object_map_size_matches_objects() {
    let f = FlightFile::from_bytes(&write_kitchen_sink()).unwrap();
    assert_eq!(f.object_map().len(), f.objects().len());
}

#[test]
fn object_items_reference_children() {
    let f = FlightFile::from_bytes(&write_kitchen_sink()).unwrap();
    let map = f.object_map();
    assert_eq!(map[&7].items[0].value_id, 3);
    assert_eq!(map[&7].items[0].key.as_deref(), Some("k"));
}

#[test]
fn mutations_accessor_in_seq_field_order() {
    let f = FlightFile::from_bytes(&write_kitchen_sink()).unwrap();
    let muts = f.mutations();
    assert_eq!(muts.len(), 2);
    assert_eq!(muts[0].seq, 0);
    assert_eq!(muts[1].seq, 3);
}

#[test]
fn mutation_value_history() {
    let f = FlightFile::from_bytes(&write_kitchen_sink()).unwrap();
    let history: Vec<String> = f
        .mutations()
        .iter()
        .map(|m| m.value.repr.clone().unwrap_or_default())
        .collect();
    assert_eq!(history, vec!["0".to_string(), "45".to_string()]);
}

#[test]
fn nondet_accessor_roundtrip() {
    let f = FlightFile::from_bytes(&write_kitchen_sink()).unwrap();
    assert_eq!(f.nondet(), sample_nondet());
}

#[test]
fn nondet_sources_preserved() {
    let f = FlightFile::from_bytes(&write_kitchen_sink()).unwrap();
    let nd = f.nondet();
    assert_eq!(nd[0].source, "time.time");
    assert_eq!(nd[1].source, "random.random");
}

#[test]
fn sources_single_block() {
    let mut buf = Vec::new();
    let mut w = FlightWriter::new(&mut buf, &HeaderMeta::new("0.0.1")).unwrap();
    w.write_block(BlockType::Source, &vec![sample_source("only.py")])
        .unwrap();
    w.finish().unwrap();
    let f = FlightFile::from_bytes(&buf).unwrap();
    assert_eq!(f.sources().len(), 1);
    assert_eq!(f.sources()[0].filename, "only.py");
}

#[test]
fn sources_multiple_blocks_all_collected() {
    let f = FlightFile::from_bytes(&write_kitchen_sink()).unwrap();
    let names: Vec<String> = f.sources().into_iter().map(|s| s.filename).collect();
    assert_eq!(names, vec!["a.py".to_string(), "b.py".to_string()]);
}

#[test]
fn sources_text_and_sha_preserved() {
    let f = FlightFile::from_bytes(&write_kitchen_sink()).unwrap();
    let s = &f.sources()[0];
    assert_eq!(s.sha1, "sha-a.py");
    assert!(s.text.contains("x = 1"));
}

#[test]
fn many_source_blocks_all_collected() {
    let mut buf = Vec::new();
    let mut w = FlightWriter::new(&mut buf, &HeaderMeta::new("0.0.1")).unwrap();
    for i in 0..25 {
        w.write_block(BlockType::Source, &vec![sample_source(&format!("f{i}.py"))])
            .unwrap();
    }
    w.finish().unwrap();
    let f = FlightFile::from_bytes(&buf).unwrap();
    assert_eq!(f.sources().len(), 25);
}

#[test]
fn aliases_finds_object_across_frames() {
    let f = FlightFile::from_bytes(&write_kitchen_sink()).unwrap();
    assert_eq!(
        f.aliases(7),
        vec![(0, "cfg".to_string()), (1, "config".to_string())]
    );
}

#[test]
fn aliases_of_shared_scalar() {
    let f = FlightFile::from_bytes(&write_kitchen_sink()).unwrap();

    assert_eq!(
        f.aliases(3),
        vec![(0, "x".to_string()), (1, "n".to_string())]
    );
}

#[test]
fn aliases_unknown_id_is_empty() {
    let f = FlightFile::from_bytes(&write_kitchen_sink()).unwrap();
    assert!(f.aliases(999_999).is_empty());
}

#[test]
fn aliases_empty_when_no_frames() {
    let f = FlightFile::from_bytes(&write_full_file()).unwrap();
    assert!(f.aliases(7).is_empty());
}

#[test]
fn meta_absent_returns_none() {
    let mut buf = Vec::new();
    let mut w = FlightWriter::new(&mut buf, &HeaderMeta::new("0.0.1")).unwrap();
    w.write_block(BlockType::EventRing, &sample_ring(3))
        .unwrap();
    w.finish().unwrap();
    let f = FlightFile::from_bytes(&buf).unwrap();
    assert!(f.meta().is_none());
}

#[test]
fn event_ring_absent_returns_none() {
    let mut buf = Vec::new();
    let mut w = FlightWriter::new(&mut buf, &HeaderMeta::new("0.0.1")).unwrap();
    w.write_block_named(BlockType::Meta, &sample_meta())
        .unwrap();
    w.finish().unwrap();
    let f = FlightFile::from_bytes(&buf).unwrap();
    assert!(f.event_ring().is_none());
}

#[test]
fn exceptions_absent_is_empty() {
    let f = FlightFile::from_bytes(&write_full_file()).unwrap();
    assert!(f.exceptions().is_empty());
}

#[test]
fn frames_absent_is_empty() {
    let f = FlightFile::from_bytes(&write_full_file()).unwrap();
    assert!(f.frames().is_empty());
}

#[test]
fn objects_absent_is_empty() {
    let f = FlightFile::from_bytes(&write_full_file()).unwrap();
    assert!(f.objects().is_empty());
}

#[test]
fn object_map_absent_is_empty() {
    let f = FlightFile::from_bytes(&write_full_file()).unwrap();
    assert!(f.object_map().is_empty());
}

#[test]
fn mutations_absent_is_empty() {
    let f = FlightFile::from_bytes(&write_full_file()).unwrap();
    assert!(f.mutations().is_empty());
}

#[test]
fn nondet_absent_is_empty() {
    let f = FlightFile::from_bytes(&write_full_file()).unwrap();
    assert!(f.nondet().is_empty());
}

#[test]
fn sources_absent_is_empty() {
    let f = FlightFile::from_bytes(&write_full_file()).unwrap();
    assert!(f.sources().is_empty());
}

#[test]
fn blocks_are_in_file_order() {
    let f = FlightFile::from_bytes(&write_kitchen_sink()).unwrap();
    let types: Vec<u8> = f.blocks.iter().map(|b| b.block_type).collect();
    assert_eq!(
        types,
        vec![
            BlockType::Meta as u8,
            BlockType::Exception as u8,
            BlockType::Frame as u8,
            BlockType::Object as u8,
            BlockType::Mutation as u8,
            BlockType::Nondet as u8,
            BlockType::Source as u8,
            BlockType::Source as u8,
            BlockType::Timeline as u8,
            BlockType::Ext as u8,
            0x42,
            BlockType::EventRing as u8,
        ]
    );
}

#[test]
fn index_block_never_surfaces_in_blocks() {
    let f = FlightFile::from_bytes(&write_kitchen_sink()).unwrap();
    assert!(f
        .blocks
        .iter()
        .all(|b| b.block_type != BlockType::Index as u8));
}

#[test]
fn block_offsets_are_ascending() {
    let f = FlightFile::from_bytes(&write_kitchen_sink()).unwrap();
    assert!(f.blocks.windows(2).all(|w| w[0].offset < w[1].offset));
}

#[test]
fn block_offset_points_at_type_byte() {
    let bytes = write_kitchen_sink();
    let f = FlightFile::from_bytes(&bytes).unwrap();
    for b in &f.blocks {
        assert_eq!(bytes[b.offset as usize], b.block_type);
    }
}

#[test]
fn raw_payload_is_decompressed_msgpack() {
    let f = FlightFile::from_bytes(&write_kitchen_sink()).unwrap();
    let meta_block = f
        .blocks
        .iter()
        .find(|b| b.block_type == BlockType::Meta as u8)
        .unwrap();
    let decoded: MetaBlock = rmp_serde::from_slice(&meta_block.payload).unwrap();
    assert_eq!(decoded, sample_meta());
}

#[test]
fn type_name_known_blocks() {
    let f = FlightFile::from_bytes(&write_kitchen_sink()).unwrap();
    let meta = f
        .blocks
        .iter()
        .find(|b| b.block_type == BlockType::Meta as u8)
        .unwrap();
    assert_eq!(meta.type_name(), "META");
}

#[test]
fn type_name_ext_block() {
    let f = FlightFile::from_bytes(&write_kitchen_sink()).unwrap();
    let ext = f
        .blocks
        .iter()
        .find(|b| b.block_type == BlockType::Ext as u8)
        .unwrap();
    assert_eq!(ext.type_name(), "EXT");
}

#[test]
fn type_name_timeline_block() {
    let f = FlightFile::from_bytes(&write_kitchen_sink()).unwrap();
    let tl = f
        .blocks
        .iter()
        .find(|b| b.block_type == BlockType::Timeline as u8)
        .unwrap();
    assert_eq!(tl.type_name(), "TIMELINE");
}

#[test]
fn type_name_unknown_block() {
    let f = FlightFile::from_bytes(&write_kitchen_sink()).unwrap();
    let unk = f.blocks.iter().find(|b| b.block_type == 0x42).unwrap();
    assert_eq!(unk.type_name(), "UNKNOWN");
}

#[test]
fn unknown_block_present_but_skipped_by_typed_accessors() {
    let f = FlightFile::from_bytes(&write_kitchen_sink()).unwrap();
    assert!(f.blocks.iter().any(|b| b.block_type == 0x42));

    assert_eq!(f.meta().unwrap(), sample_meta());
    assert_eq!(f.event_ring().unwrap(), sample_ring(30));
}

#[test]
fn first_meta_block_wins() {
    let mut buf = Vec::new();
    let mut w = FlightWriter::new(&mut buf, &HeaderMeta::new("0.0.1")).unwrap();
    let first = sample_meta();
    let mut second = sample_meta();
    second.cwd = "/other".into();
    w.write_block_named(BlockType::Meta, &first).unwrap();
    w.write_block_named(BlockType::Meta, &second).unwrap();
    w.finish().unwrap();
    let f = FlightFile::from_bytes(&buf).unwrap();
    assert_eq!(f.meta().unwrap().cwd, "/srv/app");
}

#[test]
fn first_event_ring_block_wins() {
    let mut buf = Vec::new();
    let mut w = FlightWriter::new(&mut buf, &HeaderMeta::new("0.0.1")).unwrap();
    w.write_block(BlockType::EventRing, &sample_ring(5))
        .unwrap();
    w.write_block(BlockType::EventRing, &sample_ring(99))
        .unwrap();
    w.finish().unwrap();
    let f = FlightFile::from_bytes(&buf).unwrap();
    assert_eq!(f.event_ring().unwrap().events.len(), 5);
}

#[test]
fn clean_close_uses_index() {
    let f = FlightFile::from_bytes(&write_kitchen_sink()).unwrap();
    assert!(f.used_index);
    assert!(!f.partial);
}

#[test]
fn footerless_uses_scan_and_is_not_partial() {
    let mut buf = Vec::new();
    let mut w = FlightWriter::new(&mut buf, &HeaderMeta::new("0.0.1")).unwrap();
    w.write_block_named(BlockType::Meta, &sample_meta())
        .unwrap();
    w.write_block(BlockType::EventRing, &sample_ring(3))
        .unwrap();
    w.flush().unwrap();
    drop(w);
    let f = FlightFile::from_bytes(&buf).unwrap();
    assert!(!f.used_index);
    assert!(!f.partial);
}

#[test]
fn index_and_scan_agree_on_block_count() {
    let clean = write_kitchen_sink();
    let via_index = FlightFile::from_bytes(&clean).unwrap();

    let mut scanned_bytes = clean.clone();
    let n = scanned_bytes.len();
    scanned_bytes[n - 1] = b'?';
    let via_scan = FlightFile::from_bytes(&scanned_bytes).unwrap();
    assert!(via_index.used_index);
    assert!(!via_scan.used_index);
    assert_eq!(via_index.blocks.len(), via_scan.blocks.len());
}

#[test]
fn index_and_scan_agree_on_accessor_output() {
    let clean = write_kitchen_sink();
    let via_index = FlightFile::from_bytes(&clean).unwrap();
    let mut scanned_bytes = clean.clone();
    let n = scanned_bytes.len();
    scanned_bytes[n - 1] = b'?';
    let via_scan = FlightFile::from_bytes(&scanned_bytes).unwrap();
    assert_eq!(via_index.meta(), via_scan.meta());
    assert_eq!(via_index.frames(), via_scan.frames());
    assert_eq!(via_index.objects(), via_scan.objects());
    assert_eq!(via_index.event_ring(), via_scan.event_ring());
}

#[test]
fn finish_with_no_content_blocks() {
    let mut buf = Vec::new();
    let w = FlightWriter::new(&mut buf, &HeaderMeta::new("0.0.1")).unwrap();
    w.finish().unwrap();
    let f = FlightFile::from_bytes(&buf).unwrap();
    assert!(f.used_index);
    assert!(!f.partial);
    assert!(f.blocks.is_empty());
    assert!(f.meta().is_none());
    assert!(f.event_ring().is_none());
}

#[test]
fn header_only_file_no_footer() {
    let mut buf = Vec::new();
    let w = FlightWriter::new(&mut buf, &HeaderMeta::new("0.0.1")).unwrap();
    drop(w);
    let f = FlightFile::from_bytes(&buf).unwrap();
    assert!(!f.used_index);
    assert!(!f.partial, "an empty-but-whole body is not partial");
    assert!(f.blocks.is_empty());
}

#[test]
fn broken_trailer_magic_falls_back_to_scan() {
    let mut bytes = write_kitchen_sink();
    let n = bytes.len();
    bytes[n - 2] = b'X';
    let f = FlightFile::from_bytes(&bytes).unwrap();
    assert!(!f.used_index);
    assert_eq!(f.meta().unwrap(), sample_meta());
    assert_eq!(f.event_ring().unwrap(), sample_ring(30));
}

#[test]
fn index_length_too_large_falls_back_to_scan() {
    let mut bytes = write_kitchen_sink();
    let n = bytes.len();

    bytes[n - 8] = 0xFF;
    bytes[n - 7] = 0xFF;
    bytes[n - 6] = 0xFF;
    bytes[n - 5] = 0xFF;
    let f = FlightFile::from_bytes(&bytes).unwrap();
    assert!(!f.used_index);
    assert_eq!(f.meta().unwrap(), sample_meta());
}

#[test]
fn index_start_before_body_falls_back_to_scan() {
    let mut bytes = write_kitchen_sink();
    let n = bytes.len();

    let bogus = (n - TRAILER_LEN) as u32;
    bytes[n - 8..n - 4].copy_from_slice(&bogus.to_le_bytes());
    let f = FlightFile::from_bytes(&bytes).unwrap();
    assert!(!f.used_index);
    assert_eq!(f.meta().unwrap(), sample_meta());
}

#[test]
fn index_disagreeing_with_bytes_falls_back_to_scan() {
    let clean = write_kitchen_sink();
    let f0 = FlightFile::from_bytes(&clean).unwrap();
    let meta_off = f0
        .blocks
        .iter()
        .find(|b| b.block_type == BlockType::Meta as u8)
        .unwrap()
        .offset as usize;
    let mut bytes = clean.clone();
    bytes[meta_off] = 0x42;
    let f = FlightFile::from_bytes(&bytes).unwrap();
    assert!(!f.used_index);

    assert!(f.meta().is_none());
    assert!(f.blocks.iter().any(|b| b.block_type == 0x42));
    assert_eq!(f.event_ring().unwrap(), sample_ring(30));
}

fn scratch(name: &str) -> std::path::PathBuf {
    let mut p = std::env::temp_dir();
    p.push(format!(
        "flight-reader-ext-{}-{}.flight",
        std::process::id(),
        name
    ));
    p
}

#[test]
fn open_reads_a_file_from_disk() {
    let bytes = write_kitchen_sink();
    let path = scratch("open");
    std::fs::write(&path, &bytes).unwrap();
    let f = FlightFile::open(&path).unwrap();
    assert_eq!(f.meta().unwrap(), sample_meta());
    assert_eq!(f.event_ring().unwrap(), sample_ring(30));
    std::fs::remove_file(&path).ok();
}

#[test]
fn open_nonexistent_path_is_err() {
    let path = scratch("does-not-exist-xyz");
    std::fs::remove_file(&path).ok();
    assert!(FlightFile::open(&path).is_err());
}

#[test]
fn empty_input_is_err() {
    assert!(FlightFile::from_bytes(b"").is_err());
}

#[test]
fn too_short_input_is_err() {
    assert!(FlightFile::from_bytes(b"FL").is_err());
    assert!(FlightFile::from_bytes(&[0u8; HEADER_FIXED_LEN - 1]).is_err());
}

#[test]
fn wrong_magic_is_err() {
    let mut bytes = write_full_file();
    bytes[0] = b'X';
    assert!(matches!(
        FlightFile::from_bytes(&bytes),
        Err(flight_format::FormatError::NotAFlightFile)
    ));
}

#[test]
fn garbage_bytes_are_err() {
    assert!(FlightFile::from_bytes(b"GIF89a not a flight file at all").is_err());
}

#[test]
fn future_version_rejected() {
    let mut bytes = write_full_file();
    bytes[4] = 99;
    assert!(matches!(
        FlightFile::from_bytes(&bytes),
        Err(flight_format::FormatError::UnsupportedVersion(99))
    ));
}

#[test]
fn header_declaring_more_meta_than_present_is_err() {
    let mut bytes = write_full_file();

    bytes[6..10].copy_from_slice(&(u32::MAX).to_le_bytes());
    assert!(FlightFile::from_bytes(&bytes).is_err());
}

#[test]
fn corrupt_meta_msgpack_is_err() {
    let mut bytes = Vec::new();
    bytes.extend_from_slice(flight_format::MAGIC);
    bytes.extend_from_slice(&1u16.to_le_bytes());
    bytes.extend_from_slice(&3u32.to_le_bytes());
    bytes.extend_from_slice(&[0xFF, 0xFF, 0xFF]);
    assert!(FlightFile::from_bytes(&bytes).is_err());
}

#[test]
fn exactly_header_len_with_zero_meta_parses() {
    let mut bytes = Vec::new();
    bytes.extend_from_slice(flight_format::MAGIC);
    bytes.extend_from_slice(&1u16.to_le_bytes());
    bytes.extend_from_slice(&0u32.to_le_bytes());

    let _ = FlightFile::from_bytes(&bytes);
}

#[test]
fn trailer_magic_constant_is_last_four_bytes_of_clean_file() {
    let bytes = write_kitchen_sink();
    assert_eq!(&bytes[bytes.len() - 4..], TRAILER_MAGIC);
}
