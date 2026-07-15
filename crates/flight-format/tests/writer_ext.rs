use flight_format::{
    compress, decompress, from_msgpack, to_msgpack, BlockType, FlightWriter, HeaderMeta,
    IndexEntry, MetaBlock, BLOCK_HEADER_LEN, FORMAT_VERSION, HEADER_FIXED_LEN, MAGIC, TRAILER_LEN,
    TRAILER_MAGIC,
};

fn meta() -> HeaderMeta {
    HeaderMeta {
        tool: "flight".into(),
        flight_version: "0.0.1".into(),
        created_unix_ms: 1,
    }
}

fn header_len(buf: &[u8]) -> usize {
    HEADER_FIXED_LEN + u32::from_le_bytes([buf[6], buf[7], buf[8], buf[9]]) as usize
}

fn read_block(buf: &[u8], off: usize) -> (u8, &[u8], usize) {
    let ty = buf[off];
    let len = u32::from_le_bytes([buf[off + 1], buf[off + 2], buf[off + 3], buf[off + 4]]) as usize;
    let start = off + BLOCK_HEADER_LEN;
    (ty, &buf[start..start + len], start + len)
}

#[test]
fn header_starts_with_magic() {
    let mut buf = Vec::new();
    let _ = FlightWriter::new(&mut buf, &meta()).unwrap();
    assert_eq!(&buf[0..4], MAGIC);
    assert_eq!(&buf[0..4], b"FLGT");
}

#[test]
fn header_version_is_le_u16() {
    let mut buf = Vec::new();
    let _ = FlightWriter::new(&mut buf, &meta()).unwrap();
    assert_eq!(u16::from_le_bytes([buf[4], buf[5]]), FORMAT_VERSION);
    assert_eq!(buf[4], 1);
    assert_eq!(buf[5], 0);
}

#[test]
fn header_meta_len_matches_buffer() {
    let mut buf = Vec::new();
    let _ = FlightWriter::new(&mut buf, &meta()).unwrap();
    let meta_len = u32::from_le_bytes([buf[6], buf[7], buf[8], buf[9]]) as usize;
    assert_eq!(buf.len(), HEADER_FIXED_LEN + meta_len);
}

#[test]
fn header_meta_decodes_back() {
    let m = meta();
    let mut buf = Vec::new();
    let _ = FlightWriter::new(&mut buf, &m).unwrap();
    let back: HeaderMeta = from_msgpack(&buf[HEADER_FIXED_LEN..]).unwrap();
    assert_eq!(back, m);
}

#[test]
fn header_is_named_map_marker() {
    let mut buf = Vec::new();
    let _ = FlightWriter::new(&mut buf, &meta()).unwrap();
    assert_eq!(buf[HEADER_FIXED_LEN], 0x83);
}

#[test]
fn empty_file_is_header_only() {
    let mut buf = Vec::new();
    let w = FlightWriter::new(&mut buf, &meta()).unwrap();
    drop(w);

    assert_eq!(buf.len(), header_len(&buf));
}

#[test]
fn header_meta_varies_with_content() {
    let mut a = Vec::new();
    let _ = FlightWriter::new(&mut a, &HeaderMeta::new("0.0.1")).unwrap();
    let mut b = Vec::new();
    let _ = FlightWriter::new(&mut b, &HeaderMeta::new("999.999.999")).unwrap();

    assert!(b.len() > a.len());
}

#[test]
fn write_block_msgpack_exact_framing() {
    let mut buf = Vec::new();
    let mut w = FlightWriter::new(&mut buf, &meta()).unwrap();
    let raw = to_msgpack(&MetaBlock::default()).unwrap();
    w.write_block_msgpack(0x06, &raw).unwrap();
    drop(w);

    let hl = header_len(&buf);
    let expected_payload = compress(&raw).unwrap();

    assert_eq!(buf[hl], 0x06);

    let len = u32::from_le_bytes([buf[hl + 1], buf[hl + 2], buf[hl + 3], buf[hl + 4]]) as usize;
    assert_eq!(len, expected_payload.len());

    assert_eq!(&buf[hl + 5..hl + 5 + len], expected_payload.as_slice());

    assert_eq!(buf.len(), hl + BLOCK_HEADER_LEN + expected_payload.len());
}

#[test]
fn write_block_msgpack_payload_is_compressed_msgpack() {
    let mut buf = Vec::new();
    let mut w = FlightWriter::new(&mut buf, &meta()).unwrap();
    let raw = to_msgpack(&MetaBlock {
        python_version: "3.13".into(),
        platform: "linux".into(),
        argv: vec!["a".into()],
        cwd: "/".into(),
        flight_version: "1".into(),
    })
    .unwrap();
    w.write_block_msgpack(0x01, &raw).unwrap();
    drop(w);

    let hl = header_len(&buf);
    let (ty, payload, _) = read_block(&buf, hl);
    assert_eq!(ty, 0x01);
    assert_eq!(decompress(payload).unwrap(), raw);
}

#[test]
fn write_block_msgpack_accepts_unknown_type_byte() {
    let mut buf = Vec::new();
    let mut w = FlightWriter::new(&mut buf, &meta()).unwrap();

    w.write_block_msgpack(0x42, &[0x90]).unwrap();
    drop(w);
    let hl = header_len(&buf);
    assert_eq!(buf[hl], 0x42);
    assert_eq!(BlockType::from_u8(buf[hl]), None);
}

#[test]
fn write_block_positional_roundtrips() {
    let mut buf = Vec::new();
    let mut w = FlightWriter::new(&mut buf, &meta()).unwrap();
    let m = MetaBlock {
        python_version: "3.13".into(),
        platform: "linux".into(),
        argv: vec!["python".into(), "app.py".into()],
        cwd: "/proj".into(),
        flight_version: "0.1".into(),
    };
    w.write_block(BlockType::Meta, &m).unwrap();
    drop(w);
    let hl = header_len(&buf);
    let (ty, payload, _) = read_block(&buf, hl);
    assert_eq!(ty, BlockType::Meta as u8);
    let raw = decompress(payload).unwrap();

    assert_eq!(raw[0], 0x95);
    let back: MetaBlock = from_msgpack(&raw).unwrap();
    assert_eq!(back, m);
}

#[test]
fn write_block_named_roundtrips_and_is_map() {
    let mut buf = Vec::new();
    let mut w = FlightWriter::new(&mut buf, &meta()).unwrap();
    let m = MetaBlock {
        python_version: "3.13".into(),
        platform: "linux".into(),
        argv: vec![],
        cwd: "/".into(),
        flight_version: "0.1".into(),
    };
    w.write_block_named(BlockType::Meta, &m).unwrap();
    drop(w);
    let hl = header_len(&buf);
    let (ty, payload, _) = read_block(&buf, hl);
    assert_eq!(ty, BlockType::Meta as u8);
    let raw = decompress(payload).unwrap();

    assert_eq!(raw[0], 0x85);
    let back: MetaBlock = from_msgpack(&raw).unwrap();
    assert_eq!(back, m);
}

#[test]
fn multiple_blocks_are_framed_in_order() {
    let mut buf = Vec::new();
    let mut w = FlightWriter::new(&mut buf, &meta()).unwrap();
    w.write_block_msgpack(0x01, &[0x90]).unwrap();
    w.write_block_msgpack(0x06, &[0x91, 0x01]).unwrap();
    w.write_block_msgpack(0x7F, &[0x92, 0x01, 0x02]).unwrap();
    drop(w);

    let hl = header_len(&buf);
    let (t0, _p0, o1) = read_block(&buf, hl);
    let (t1, _p1, o2) = read_block(&buf, o1);
    let (t2, _p2, o3) = read_block(&buf, o2);
    assert_eq!((t0, t1, t2), (0x01, 0x06, 0x7F));
    assert_eq!(o3, buf.len());
}

#[test]
fn offset_accounting_matches_finish_index() {
    let mut buf = Vec::new();
    let mut w = FlightWriter::new(&mut buf, &meta()).unwrap();
    for i in 0..5u8 {
        w.write_block_msgpack(0x06, &vec![0x90; (i as usize) + 1])
            .unwrap();
    }
    let _ = w.finish().unwrap();

    let entries = decode_index(&buf);
    for e in &entries {
        let off = e.offset as usize;
        assert_eq!(buf[off], e.block_type, "type byte mismatch at {off}");
        let len = u32::from_le_bytes([buf[off + 1], buf[off + 2], buf[off + 3], buf[off + 4]]);
        assert_eq!(len, e.payload_len, "len field mismatch at {off}");
    }
}

fn decode_index(buf: &[u8]) -> Vec<IndexEntry> {
    let n = buf.len();
    assert_eq!(&buf[n - 4..], TRAILER_MAGIC);
    let index_total_len =
        u32::from_le_bytes([buf[n - 8], buf[n - 7], buf[n - 6], buf[n - 5]]) as usize;
    let index_start = n - TRAILER_LEN - index_total_len;
    assert_eq!(buf[index_start], BlockType::Index as u8);
    let payload_len = u32::from_le_bytes([
        buf[index_start + 1],
        buf[index_start + 2],
        buf[index_start + 3],
        buf[index_start + 4],
    ]) as usize;
    let start = index_start + BLOCK_HEADER_LEN;
    let raw = decompress(&buf[start..start + payload_len]).unwrap();
    from_msgpack(&raw).unwrap()
}

#[test]
fn finish_appends_trailer_magic() {
    let mut buf = Vec::new();
    let mut w = FlightWriter::new(&mut buf, &meta()).unwrap();
    w.write_block_named(BlockType::Meta, &MetaBlock::default())
        .unwrap();
    let _ = w.finish().unwrap();
    assert_eq!(&buf[buf.len() - 4..], TRAILER_MAGIC);
}

#[test]
fn finish_trailer_points_at_index_block() {
    let mut buf = Vec::new();
    let mut w = FlightWriter::new(&mut buf, &meta()).unwrap();
    w.write_block_named(BlockType::Meta, &MetaBlock::default())
        .unwrap();
    let _ = w.finish().unwrap();
    let n = buf.len();
    let index_total_len =
        u32::from_le_bytes([buf[n - 8], buf[n - 7], buf[n - 6], buf[n - 5]]) as usize;
    let index_start = n - TRAILER_LEN - index_total_len;
    assert_eq!(buf[index_start], BlockType::Index as u8);
}

#[test]
fn finish_index_contains_all_prior_blocks_not_itself() {
    let mut buf = Vec::new();
    let mut w = FlightWriter::new(&mut buf, &meta()).unwrap();
    w.write_block_msgpack(0x01, &[0x90]).unwrap();
    w.write_block_msgpack(0x06, &[0x90]).unwrap();
    w.write_block_msgpack(0x09, &[0x90]).unwrap();
    let _ = w.finish().unwrap();

    let entries = decode_index(&buf);

    assert_eq!(entries.len(), 3);
    assert_eq!(entries[0].block_type, 0x01);
    assert_eq!(entries[1].block_type, 0x06);
    assert_eq!(entries[2].block_type, 0x09);
    assert!(entries
        .iter()
        .all(|e| e.block_type != BlockType::Index as u8));
}

#[test]
fn finish_index_does_not_reference_itself() {
    let mut buf = Vec::new();
    let mut w = FlightWriter::new(&mut buf, &meta()).unwrap();
    w.write_block_msgpack(0x06, &[0x90]).unwrap();
    let _ = w.finish().unwrap();

    let entries = decode_index(&buf);
    let n = buf.len();
    let index_total_len =
        u32::from_le_bytes([buf[n - 8], buf[n - 7], buf[n - 6], buf[n - 5]]) as usize;
    let index_start = (n - TRAILER_LEN - index_total_len) as u64;

    for e in &entries {
        assert!(e.offset < index_start);
    }
}

#[test]
fn finish_first_entry_offset_is_header_len() {
    let mut buf = Vec::new();
    let mut w = FlightWriter::new(&mut buf, &meta()).unwrap();
    w.write_block_msgpack(0x06, &[0x90]).unwrap();
    let _ = w.finish().unwrap();
    let entries = decode_index(&buf);
    assert_eq!(entries[0].offset as usize, header_len(&buf));
}

#[test]
fn finish_empty_file_index_is_empty() {
    let mut buf = Vec::new();
    let w = FlightWriter::new(&mut buf, &meta()).unwrap();
    let _ = w.finish().unwrap();
    let entries = decode_index(&buf);
    assert!(entries.is_empty());
}

#[test]
fn finish_index_total_len_covers_the_index_block() {
    let mut buf = Vec::new();
    let mut w = FlightWriter::new(&mut buf, &meta()).unwrap();
    w.write_block_msgpack(0x06, &[0x90]).unwrap();
    let _ = w.finish().unwrap();
    let n = buf.len();
    let index_total_len =
        u32::from_le_bytes([buf[n - 8], buf[n - 7], buf[n - 6], buf[n - 5]]) as usize;
    let index_start = n - TRAILER_LEN - index_total_len;

    let payload_len = u32::from_le_bytes([
        buf[index_start + 1],
        buf[index_start + 2],
        buf[index_start + 3],
        buf[index_start + 4],
    ]) as usize;
    assert_eq!(BLOCK_HEADER_LEN + payload_len, index_total_len);
}

#[test]
fn file_without_finish_has_no_trailer() {
    let mut buf = Vec::new();
    let mut w = FlightWriter::new(&mut buf, &meta()).unwrap();
    w.write_block_named(BlockType::Meta, &MetaBlock::default())
        .unwrap();
    w.flush().unwrap();
    drop(w);
    assert_ne!(&buf[buf.len() - 4..], TRAILER_MAGIC);
}

#[test]
fn many_blocks_stress_index_count() {
    let mut buf = Vec::new();
    let mut w = FlightWriter::new(&mut buf, &meta()).unwrap();
    for i in 0..200 {
        let payload = to_msgpack(&MetaBlock {
            python_version: format!("3.13.{i}"),
            platform: "linux".into(),
            argv: vec![],
            cwd: "/".into(),
            flight_version: "1".into(),
        })
        .unwrap();
        w.write_block_msgpack(0x01, &payload).unwrap();
    }
    let _ = w.finish().unwrap();
    let entries = decode_index(&buf);
    assert_eq!(entries.len(), 200);
}

#[test]
fn finish_index_offsets_are_strictly_increasing() {
    let mut buf = Vec::new();
    let mut w = FlightWriter::new(&mut buf, &meta()).unwrap();
    for _ in 0..10 {
        w.write_block_msgpack(0x06, &[0x90]).unwrap();
    }
    let _ = w.finish().unwrap();
    let entries = decode_index(&buf);
    for pair in entries.windows(2) {
        assert!(pair[1].offset > pair[0].offset);
    }
}

#[test]
fn create_writes_readable_file_on_disk() {
    use std::io::Read;
    let mut path = std::env::temp_dir();
    path.push(format!("flight_writer_test_{}.flight", std::process::id()));
    {
        let mut w = FlightWriter::create(&path, &meta()).unwrap();
        w.write_block_named(BlockType::Meta, &MetaBlock::default())
            .unwrap();
        let _ = w.finish().unwrap();
    }
    let mut bytes = Vec::new();
    std::fs::File::open(&path)
        .unwrap()
        .read_to_end(&mut bytes)
        .unwrap();
    std::fs::remove_file(&path).ok();

    assert_eq!(&bytes[0..4], MAGIC);
    assert_eq!(&bytes[bytes.len() - 4..], TRAILER_MAGIC);
    let entries = decode_index(&bytes);

    assert_eq!(entries.len(), 1);
    assert_eq!(entries.last().unwrap().block_type, BlockType::Meta as u8);
}

#[test]
fn writer_returns_underlying_on_finish() {
    let buf = Vec::new();
    let w = FlightWriter::new(buf, &meta()).unwrap();
    let out = w.finish().unwrap();
    assert_eq!(&out[0..4], MAGIC);
    assert_eq!(&out[out.len() - 4..], TRAILER_MAGIC);
}
