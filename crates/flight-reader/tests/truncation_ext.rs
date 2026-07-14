use std::collections::HashMap;

use flight_format::{
    BlockType, CodeInfo, Event, EventKind, ExceptionLink, FlightWriter, FrameInfo, HeaderMeta,
    MetaBlock, ObjectItem, ObjectNode, RingPayload, SourceFile,
};
use flight_reader::FlightFile;

fn meta() -> MetaBlock {
    MetaBlock {
        python_version: "3.13.1".into(),
        platform: "Linux".into(),
        argv: vec!["a.py".into()],
        cwd: "/x".into(),
        flight_version: "0.0.1".into(),
    }
}

fn ring(n: u64) -> RingPayload {
    let mut codes = HashMap::new();
    codes.insert(
        1u64,
        CodeInfo {
            file: "a.py".into(),
            qualname: "f".into(),
            first_line: 1,
        },
    );
    RingPayload {
        codes,
        events: (0..n)
            .map(|i| Event::new(EventKind::Line, 0, i as u32, 1, i))
            .collect(),
        wrapped: false,
    }
}


fn full() -> Vec<u8> {
    let mut buf = Vec::new();
    let mut w = FlightWriter::new(&mut buf, &HeaderMeta::new("0.0.1")).unwrap();
    w.write_block_named(BlockType::Meta, &meta()).unwrap();
    w.write_block(
        BlockType::Exception,
        &vec![ExceptionLink {
            exc_type: "ValueError".into(),
            message: "x".into(),
            relation: "head".into(),
        }],
    )
    .unwrap();
    w.write_block(
        BlockType::Frame,
        &vec![FrameInfo {
            file: "a.py".into(),
            qualname: "f".into(),
            lineno: 3,
            first_lineno: 1,
            locals: vec![("cfg".into(), 7)],
        }],
    )
    .unwrap();
    w.write_block(
        BlockType::Object,
        &vec![ObjectNode {
            id: 7,
            kind: "dict".into(),
            repr: None,
            type_name: None,
            length: Some(0),
            truncated: false,
            items: vec![ObjectItem {
                key: Some("k".into()),
                value_id: 7,
            }],
        }],
    )
    .unwrap();
    w.write_block(
        BlockType::Source,
        &vec![SourceFile {
            filename: "a.py".into(),
            sha1: "h".into(),
            text: "x=1\n".into(),
        }],
    )
    .unwrap();
    w.write_block(
        BlockType::Source,
        &vec![SourceFile {
            filename: "b.py".into(),
            sha1: "h2".into(),
            text: "y=2\n".into(),
        }],
    )
    .unwrap();
    w.write_block(BlockType::EventRing, &ring(200)).unwrap();
    w.finish().unwrap();
    buf
}


#[test]
fn truncation_at_every_offset_never_panics() {
    let bytes = full();
    for cut in 0..=bytes.len() {

        let _ = FlightFile::from_bytes(&bytes[..cut]);
    }
}

#[test]
fn truncation_at_every_offset_error_only_in_header_region() {
    let bytes = full();


    let header_len = {
        let meta_len = u32::from_le_bytes([bytes[6], bytes[7], bytes[8], bytes[9]]) as usize;
        flight_format::HEADER_FIXED_LEN + meta_len
    };
    for cut in 0..=bytes.len() {
        match FlightFile::from_bytes(&bytes[..cut]) {
            Err(_) => assert!(
                cut < header_len,
                "hard error at offset {cut} but header ends at {header_len}"
            ),
            Ok(_) => {}
        }
    }
}

#[test]
fn truncation_after_header_yields_ok_readable_file() {
    let bytes = full();
    let header_len = {
        let meta_len = u32::from_le_bytes([bytes[6], bytes[7], bytes[8], bytes[9]]) as usize;
        flight_format::HEADER_FIXED_LEN + meta_len
    };

    let f = FlightFile::from_bytes(&bytes[..header_len]).unwrap();
    assert!(f.blocks.is_empty());
    assert_eq!(f.format_version, 1);
}

#[test]
fn every_truncation_block_count_is_monotone_nondecreasing() {

    let bytes = full();
    let header_len = {
        let meta_len = u32::from_le_bytes([bytes[6], bytes[7], bytes[8], bytes[9]]) as usize;
        flight_format::HEADER_FIXED_LEN + meta_len
    };
    let mut last = 0usize;
    for cut in header_len..=bytes.len() {
        if let Ok(f) = FlightFile::from_bytes(&bytes[..cut]) {
            assert!(
                f.blocks.len() >= last,
                "block count went backwards at cut {cut}: {} < {last}",
                f.blocks.len()
            );
            last = f.blocks.len();
        }
    }
}

#[test]
fn every_truncation_yields_a_prefix_of_the_full_block_list() {


    let bytes = full();
    let full_f = FlightFile::from_bytes(&bytes).unwrap();
    for cut in 0..=bytes.len() {
        if let Ok(f) = FlightFile::from_bytes(&bytes[..cut]) {
            assert!(f.blocks.len() <= full_f.blocks.len());
            for (i, b) in f.blocks.iter().enumerate() {
                assert_eq!(b.block_type, full_f.blocks[i].block_type, "cut {cut} block {i}");
                assert_eq!(b.offset, full_f.blocks[i].offset, "cut {cut} block {i}");
                assert_eq!(b.payload, full_f.blocks[i].payload, "cut {cut} block {i}");
            }
        }
    }
}


#[test]
fn footer_fully_gone_keeps_all_data_not_partial() {
    let bytes = full();
    let last_data_end = {
        let n = bytes.len();
        let index_total =
            u32::from_le_bytes([bytes[n - 8], bytes[n - 7], bytes[n - 6], bytes[n - 5]]) as usize;
        n - 8 - index_total
    };
    let full_f = FlightFile::from_bytes(&bytes).unwrap();
    let cut_f = FlightFile::from_bytes(&bytes[..last_data_end]).unwrap();
    assert_eq!(cut_f.blocks.len(), full_f.blocks.len());
    assert!(!cut_f.partial);
    assert!(!cut_f.used_index);
    assert_eq!(cut_f.event_ring(), full_f.event_ring());
}


#[test]
fn mid_ring_block_keeps_earlier_blocks_and_flags_partial() {
    let bytes = full();
    let full_f = FlightFile::from_bytes(&bytes).unwrap();
    let ring_off = full_f
        .blocks
        .iter()
        .find(|b| b.block_type == BlockType::EventRing as u8)
        .unwrap()
        .offset as usize;
    let f = FlightFile::from_bytes(&bytes[..ring_off + 3]).unwrap();
    assert!(f.partial);
    assert!(f.meta().is_some());
    assert!(f.frames().len() == 1);
    assert!(f.event_ring().is_none());
}

#[test]
fn cut_one_byte_into_first_block_loses_all_blocks() {
    let bytes = full();
    let header_len = {
        let meta_len = u32::from_le_bytes([bytes[6], bytes[7], bytes[8], bytes[9]]) as usize;
        flight_format::HEADER_FIXED_LEN + meta_len
    };

    let f = FlightFile::from_bytes(&bytes[..header_len + 1]).unwrap();
    assert!(f.partial);
    assert!(f.blocks.is_empty());
}

#[test]
fn cut_inside_block_header_is_partial() {
    let bytes = full();
    let header_len = {
        let meta_len = u32::from_le_bytes([bytes[6], bytes[7], bytes[8], bytes[9]]) as usize;
        flight_format::HEADER_FIXED_LEN + meta_len
    };

    let f = FlightFile::from_bytes(&bytes[..header_len + 3]).unwrap();
    assert!(f.partial);
    assert!(f.blocks.is_empty());
}

#[test]
fn cut_after_first_block_keeps_exactly_it() {
    let bytes = full();
    let full_f = FlightFile::from_bytes(&bytes).unwrap();

    let second_off = full_f.blocks[1].offset as usize;
    let f = FlightFile::from_bytes(&bytes[..second_off]).unwrap();
    assert_eq!(f.blocks.len(), 1);
    assert_eq!(f.blocks[0].block_type, BlockType::Meta as u8);
    assert!(!f.partial, "clean cut on a block boundary is whole");
    assert_eq!(f.meta().unwrap(), meta());
}


#[test]
fn corrupt_last_block_payload_drops_it_others_survive() {
    let mut bytes = full();
    let f0 = FlightFile::from_bytes(&bytes).unwrap();
    let ring_off = f0
        .blocks
        .iter()
        .find(|b| b.block_type == BlockType::EventRing as u8)
        .unwrap()
        .offset as usize;

    for i in 0..16 {
        bytes[ring_off + 5 + 4 + i] ^= 0xFF;
    }
    let f = FlightFile::from_bytes(&bytes).unwrap();

    assert!(f.meta().is_some());

    assert!(f.event_ring().is_none() || f.partial || f.used_index);
}

#[test]
fn corrupt_middle_block_payload_never_panics() {
    let mut bytes = full();
    let f0 = FlightFile::from_bytes(&bytes).unwrap();
    let frame_off = f0
        .blocks
        .iter()
        .find(|b| b.block_type == BlockType::Frame as u8)
        .unwrap()
        .offset as usize;
    for i in 0..8 {
        bytes[frame_off + 5 + 4 + i] ^= 0xAA;
    }

    let _ = FlightFile::from_bytes(&bytes);
}

#[test]
fn oversized_block_length_field_is_partial_not_panic() {
    let mut bytes = full();
    let f0 = FlightFile::from_bytes(&bytes).unwrap();
    let meta_off = f0.blocks[0].offset as usize;


    bytes[meta_off + 1..meta_off + 5].copy_from_slice(&(u32::MAX).to_le_bytes());
    let _ = FlightFile::from_bytes(&bytes);
}


#[test]
fn flip_every_byte_of_trailer_region_never_panics() {
    let base = full();
    let n = base.len();

    for pos in n.saturating_sub(24)..n {
        for bit in 0..8u8 {
            let mut bytes = base.clone();
            bytes[pos] ^= 1 << bit;
            let _ = FlightFile::from_bytes(&bytes);
        }
    }
}

#[test]
fn flip_every_byte_of_header_region_never_panics() {
    let base = full();
    let header_len = {
        let meta_len = u32::from_le_bytes([base[6], base[7], base[8], base[9]]) as usize;
        flight_format::HEADER_FIXED_LEN + meta_len
    };
    for pos in 0..header_len {
        for bit in 0..8u8 {
            let mut bytes = base.clone();
            bytes[pos] ^= 1 << bit;
            let _ = FlightFile::from_bytes(&bytes);
        }
    }
}

#[test]
fn random_byte_flips_across_whole_file_never_panic() {


    let base = full();
    let n = base.len();
    let mut state = 0x1234_5678u64;
    for _ in 0..2000 {
        state = state.wrapping_mul(6364136223846793005).wrapping_add(1442695040888963407);
        let pos = (state >> 33) as usize % n;
        let val = (state & 0xFF) as u8;
        let mut bytes = base.clone();
        bytes[pos] ^= val;
        let _ = FlightFile::from_bytes(&bytes);
    }
}

#[test]
fn truncated_then_reparsed_is_stable() {

    let bytes = full();
    for cut in (0..bytes.len()).step_by(7) {
        let a = FlightFile::from_bytes(&bytes[..cut]);
        let b = FlightFile::from_bytes(&bytes[..cut]);
        match (a, b) {
            (Ok(fa), Ok(fb)) => {
                assert_eq!(fa.blocks.len(), fb.blocks.len());
                assert_eq!(fa.partial, fb.partial);
                assert_eq!(fa.used_index, fb.used_index);
            }
            (Err(_), Err(_)) => {}
            _ => panic!("nondeterministic parse at cut {cut}"),
        }
    }
}

#[test]
fn single_extra_trailing_garbage_byte_falls_back_to_scan() {


    let mut bytes = full();
    bytes.push(0xEE);
    let f = FlightFile::from_bytes(&bytes).unwrap();
    assert!(!f.used_index);
    assert!(f.meta().is_some());
}
