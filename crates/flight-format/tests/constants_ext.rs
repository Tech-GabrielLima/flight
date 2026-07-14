use flight_format::{
    Event, EventKind, BLOCK_HEADER_LEN, FORMAT_VERSION, HEADER_FIXED_LEN, MAGIC, TRAILER_LEN,
    TRAILER_MAGIC, ZSTD_LEVEL,
};

#[test]
fn header_fixed_len_is_10() {
    assert_eq!(HEADER_FIXED_LEN, 10);
}

#[test]
fn header_fixed_len_composition() {

    assert_eq!(HEADER_FIXED_LEN, 4 + 2 + 4);
}

#[test]
fn block_header_len_is_5() {
    assert_eq!(BLOCK_HEADER_LEN, 5);
}

#[test]
fn block_header_len_composition() {

    assert_eq!(BLOCK_HEADER_LEN, 1 + 4);
}

#[test]
fn trailer_len_is_8() {
    assert_eq!(TRAILER_LEN, 8);
}

#[test]
fn trailer_len_composition() {

    assert_eq!(TRAILER_LEN, 4 + 4);
}

#[test]
fn format_version_is_1() {
    assert_eq!(FORMAT_VERSION, 1);
}

#[test]
fn magic_is_flgt() {
    assert_eq!(MAGIC, b"FLGT");
    assert_eq!(MAGIC.len(), 4);
}

#[test]
fn trailer_magic_is_tlgf() {
    assert_eq!(TRAILER_MAGIC, b"TLGF");
    assert_eq!(TRAILER_MAGIC.len(), 4);
}

#[test]
fn magic_and_trailer_magic_differ() {
    assert_ne!(MAGIC, TRAILER_MAGIC);

    assert_ne!(&MAGIC[..], &TRAILER_MAGIC[..]);
}

#[test]
fn zstd_level_is_3() {
    assert_eq!(ZSTD_LEVEL, 3);
}

#[test]
fn version_le_bytes_are_1_0() {
    assert_eq!(FORMAT_VERSION.to_le_bytes(), [1, 0]);
}

#[test]
fn event_is_24_bytes() {
    assert_eq!(std::mem::size_of::<Event>(), 24);
}

#[test]
fn event_alignment_is_8() {

    assert_eq!(std::mem::align_of::<Event>(), 8);
}

#[test]
fn event_new_sets_raw_kind() {
    let e = Event::new(EventKind::Raise, 2, 10, 55, 77);
    assert_eq!(e.kind, EventKind::Raise as u8);
    assert_eq!(e.thread, 2);
    assert_eq!(e.line, 10);
    assert_eq!(e.code_id, 55);
    assert_eq!(e.tstamp, 77);
}

#[test]
fn header_fixed_len_leaves_room_for_all_fields() {

    let magic = 4usize;
    let version = std::mem::size_of::<u16>();
    let metalen = std::mem::size_of::<u32>();
    assert_eq!(magic + version + metalen, HEADER_FIXED_LEN);
}
