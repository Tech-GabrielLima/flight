use flight_format::{BlockType, Event, EventKind};


#[test]
fn block_meta_from_u8() {
    assert_eq!(BlockType::from_u8(0x01), Some(BlockType::Meta));
    assert_eq!(BlockType::Meta as u8, 0x01);
}
#[test]
fn block_source_from_u8() {
    assert_eq!(BlockType::from_u8(0x02), Some(BlockType::Source));
    assert_eq!(BlockType::Source as u8, 0x02);
}
#[test]
fn block_exception_from_u8() {
    assert_eq!(BlockType::from_u8(0x03), Some(BlockType::Exception));
    assert_eq!(BlockType::Exception as u8, 0x03);
}
#[test]
fn block_frame_from_u8() {
    assert_eq!(BlockType::from_u8(0x04), Some(BlockType::Frame));
    assert_eq!(BlockType::Frame as u8, 0x04);
}
#[test]
fn block_object_from_u8() {
    assert_eq!(BlockType::from_u8(0x05), Some(BlockType::Object));
    assert_eq!(BlockType::Object as u8, 0x05);
}
#[test]
fn block_eventring_from_u8() {
    assert_eq!(BlockType::from_u8(0x06), Some(BlockType::EventRing));
    assert_eq!(BlockType::EventRing as u8, 0x06);
}
#[test]
fn block_mutation_from_u8() {
    assert_eq!(BlockType::from_u8(0x07), Some(BlockType::Mutation));
    assert_eq!(BlockType::Mutation as u8, 0x07);
}
#[test]
fn block_timeline_from_u8() {
    assert_eq!(BlockType::from_u8(0x08), Some(BlockType::Timeline));
    assert_eq!(BlockType::Timeline as u8, 0x08);
}
#[test]
fn block_nondet_from_u8() {
    assert_eq!(BlockType::from_u8(0x09), Some(BlockType::Nondet));
    assert_eq!(BlockType::Nondet as u8, 0x09);
}
#[test]
fn block_index_from_u8() {
    assert_eq!(BlockType::from_u8(0x70), Some(BlockType::Index));
    assert_eq!(BlockType::Index as u8, 0x70);
}
#[test]
fn block_ext_from_u8() {
    assert_eq!(BlockType::from_u8(0x7F), Some(BlockType::Ext));
    assert_eq!(BlockType::Ext as u8, 0x7F);
}


#[test]
fn block_name_meta() {
    assert_eq!(BlockType::Meta.name(), "META");
}
#[test]
fn block_name_source() {
    assert_eq!(BlockType::Source.name(), "SOURCE");
}
#[test]
fn block_name_exception() {
    assert_eq!(BlockType::Exception.name(), "EXCEPTION");
}
#[test]
fn block_name_frame() {
    assert_eq!(BlockType::Frame.name(), "FRAME");
}
#[test]
fn block_name_object() {
    assert_eq!(BlockType::Object.name(), "OBJECT");
}
#[test]
fn block_name_eventring() {
    assert_eq!(BlockType::EventRing.name(), "EVENT_RING");
}
#[test]
fn block_name_mutation() {
    assert_eq!(BlockType::Mutation.name(), "MUTATION");
}
#[test]
fn block_name_timeline() {
    assert_eq!(BlockType::Timeline.name(), "TIMELINE");
}
#[test]
fn block_name_nondet() {
    assert_eq!(BlockType::Nondet.name(), "NONDET");
}
#[test]
fn block_name_index() {
    assert_eq!(BlockType::Index.name(), "INDEX");
}
#[test]
fn block_name_ext() {
    assert_eq!(BlockType::Ext.name(), "EXT");
}

const ALL_BLOCKS: [BlockType; 11] = [
    BlockType::Meta,
    BlockType::Source,
    BlockType::Exception,
    BlockType::Frame,
    BlockType::Object,
    BlockType::EventRing,
    BlockType::Mutation,
    BlockType::Timeline,
    BlockType::Nondet,
    BlockType::Index,
    BlockType::Ext,
];

#[test]
fn block_roundtrip_all_variants() {
    for b in ALL_BLOCKS {
        assert_eq!(BlockType::from_u8(b as u8), Some(b));
    }
}

#[test]
fn block_names_are_all_unique() {
    let names: Vec<&str> = ALL_BLOCKS.iter().map(|b| b.name()).collect();
    for i in 0..names.len() {
        for j in (i + 1)..names.len() {
            assert_ne!(names[i], names[j], "duplicate name at {i},{j}");
        }
    }
}

#[test]
fn block_names_are_nonempty_uppercase() {
    for b in ALL_BLOCKS {
        let n = b.name();
        assert!(!n.is_empty());
        assert!(n.chars().all(|c| c.is_ascii_uppercase() || c == '_'));
    }
}

#[test]
fn block_full_sweep_only_valid_bytes_map() {
    let valid: [u8; 11] = [
        0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07, 0x08, 0x09, 0x70, 0x7F,
    ];
    for v in 0u16..=255 {
        let v = v as u8;
        let got = BlockType::from_u8(v);
        if valid.contains(&v) {
            assert!(got.is_some(), "byte {v:#x} should be Some");
            assert_eq!(got.unwrap() as u8, v);
        } else {
            assert_eq!(got, None, "byte {v:#x} should be None");
        }
    }
}

#[test]
fn block_invalid_zero_is_none() {
    assert_eq!(BlockType::from_u8(0x00), None);
}
#[test]
fn block_invalid_0x0a_is_none() {
    assert_eq!(BlockType::from_u8(0x0A), None);
}
#[test]
fn block_invalid_gap_0x0a_to_0x6f_all_none() {
    for v in 0x0Au8..=0x6F {
        assert_eq!(BlockType::from_u8(v), None, "byte {v:#x}");
    }
}
#[test]
fn block_invalid_0x71_to_0x7e_all_none() {
    for v in 0x71u8..=0x7E {
        assert_eq!(BlockType::from_u8(v), None, "byte {v:#x}");
    }
}
#[test]
fn block_invalid_high_bytes_none() {
    for v in 0x80u8..=0xFF {
        assert_eq!(BlockType::from_u8(v), None, "byte {v:#x}");
    }
}
#[test]
fn block_type_is_copy_and_eq() {
    let a = BlockType::Frame;
    let b = a;
    assert_eq!(a, b);
    assert_ne!(BlockType::Frame, BlockType::Object);
}


#[test]
fn event_pystart_from_u8() {
    assert_eq!(EventKind::from_u8(1), Some(EventKind::PyStart));
    assert_eq!(EventKind::PyStart as u8, 1);
}
#[test]
fn event_pyreturn_from_u8() {
    assert_eq!(EventKind::from_u8(2), Some(EventKind::PyReturn));
    assert_eq!(EventKind::PyReturn as u8, 2);
}
#[test]
fn event_line_from_u8() {
    assert_eq!(EventKind::from_u8(3), Some(EventKind::Line));
    assert_eq!(EventKind::Line as u8, 3);
}
#[test]
fn event_raise_from_u8() {
    assert_eq!(EventKind::from_u8(4), Some(EventKind::Raise));
    assert_eq!(EventKind::Raise as u8, 4);
}
#[test]
fn event_reraise_from_u8() {
    assert_eq!(EventKind::from_u8(5), Some(EventKind::Reraise));
    assert_eq!(EventKind::Reraise as u8, 5);
}
#[test]
fn event_pyunwind_from_u8() {
    assert_eq!(EventKind::from_u8(6), Some(EventKind::PyUnwind));
    assert_eq!(EventKind::PyUnwind as u8, 6);
}

#[test]
fn eventkind_name_pystart() {
    assert_eq!(EventKind::PyStart.name(), "PY_START");
}
#[test]
fn eventkind_name_pyreturn() {
    assert_eq!(EventKind::PyReturn.name(), "PY_RETURN");
}
#[test]
fn eventkind_name_line() {
    assert_eq!(EventKind::Line.name(), "LINE");
}
#[test]
fn eventkind_name_raise() {
    assert_eq!(EventKind::Raise.name(), "RAISE");
}
#[test]
fn eventkind_name_reraise() {
    assert_eq!(EventKind::Reraise.name(), "RERAISE");
}
#[test]
fn eventkind_name_pyunwind() {
    assert_eq!(EventKind::PyUnwind.name(), "PY_UNWIND");
}

const ALL_KINDS: [EventKind; 6] = [
    EventKind::PyStart,
    EventKind::PyReturn,
    EventKind::Line,
    EventKind::Raise,
    EventKind::Reraise,
    EventKind::PyUnwind,
];

#[test]
fn eventkind_roundtrip_all_variants() {
    for k in ALL_KINDS {
        assert_eq!(EventKind::from_u8(k as u8), Some(k));
    }
}

#[test]
fn eventkind_names_all_unique() {
    let names: Vec<&str> = ALL_KINDS.iter().map(|k| k.name()).collect();
    for i in 0..names.len() {
        for j in (i + 1)..names.len() {
            assert_ne!(names[i], names[j]);
        }
    }
}

#[test]
fn eventkind_full_sweep_only_1_to_6_valid() {
    for v in 0u16..=255 {
        let v = v as u8;
        let got = EventKind::from_u8(v);
        if (1..=6).contains(&v) {
            assert!(got.is_some());
            assert_eq!(got.unwrap() as u8, v);
        } else {
            assert_eq!(got, None, "byte {v:#x}");
        }
    }
}

#[test]
fn eventkind_invalid_zero_none() {
    assert_eq!(EventKind::from_u8(0), None);
}
#[test]
fn eventkind_invalid_seven_none() {
    assert_eq!(EventKind::from_u8(7), None);
}
#[test]
fn eventkind_invalid_high_bytes_none() {
    for v in 7u8..=255 {
        assert_eq!(EventKind::from_u8(v), None, "byte {v}");
    }
}
#[test]
fn eventkind_is_copy_and_eq() {
    let a = EventKind::Line;
    let b = a;
    assert_eq!(a, b);
    assert_ne!(EventKind::Line, EventKind::Raise);
}

#[test]
fn event_kind_helper_resolves_valid_raw() {
    for k in ALL_KINDS {
        let e = Event::new(k, 0, 0, 0, 0);
        assert_eq!(e.kind(), Some(k));
        assert_eq!(e.kind, k as u8);
    }
}

#[test]
fn event_kind_helper_none_for_bad_raw() {

    let e = Event {
        kind: 200,
        thread: 0,
        line: 0,
        code_id: 0,
        tstamp: 0,
    };
    assert_eq!(e.kind(), None);
}
