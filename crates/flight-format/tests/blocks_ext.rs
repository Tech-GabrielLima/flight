//! msgpack round-trips for the block payload structs: `MetaBlock`,
//! `RingPayload`, `Mutation`/`MutationValue`, `NonDetEvent`, `ExceptionLink`,
//! `FrameInfo`, `ObjectNode`/`ObjectItem`, and `SourceFile` — across empty,
//! unicode, large, and boundary field values.

use std::collections::HashMap;

use flight_format::{
    from_msgpack, to_msgpack, CodeInfo, Event, EventKind, ExceptionLink, FrameInfo, MetaBlock,
    Mutation, MutationValue, NonDetEvent, ObjectItem, ObjectNode, RingPayload, SourceFile,
};

fn rt<T>(v: &T) -> T
where
    T: serde::Serialize + serde::de::DeserializeOwned,
{
    from_msgpack(&to_msgpack(v).unwrap()).unwrap()
}

// ------------------------------------------------------------------
// MetaBlock
// ------------------------------------------------------------------

#[test]
fn metablock_rt_default() {
    let m = MetaBlock::default();
    assert_eq!(rt(&m), m);
}

#[test]
fn metablock_rt_full() {
    let m = MetaBlock {
        python_version: "3.13.1".into(),
        platform: "linux".into(),
        argv: vec!["python".into(), "-m".into(), "app".into()],
        cwd: "/home/user/proj".into(),
        flight_version: "0.1.1".into(),
    };
    assert_eq!(rt(&m), m);
}

#[test]
fn metablock_rt_empty_argv() {
    let m = MetaBlock {
        python_version: "3.13".into(),
        platform: "darwin".into(),
        argv: vec![],
        cwd: "/".into(),
        flight_version: "9".into(),
    };
    assert_eq!(rt(&m), m);
}

#[test]
fn metablock_rt_many_argv() {
    let m = MetaBlock {
        python_version: "3.13".into(),
        platform: "win32".into(),
        argv: (0..500).map(|i| format!("arg{i}")).collect(),
        cwd: "C:\\proj".into(),
        flight_version: "1".into(),
    };
    assert_eq!(rt(&m), m);
}

#[test]
fn metablock_rt_unicode() {
    let m = MetaBlock {
        python_version: "3.13 — PyPy".into(),
        platform: "linux-日本語".into(),
        argv: vec!["café".into(), "naïve".into(), "😀".into()],
        cwd: "/tmp/проект".into(),
        flight_version: "λ".into(),
    };
    assert_eq!(rt(&m), m);
}

#[test]
fn metablock_positional_encoding_is_fixarray_5() {
    // to_msgpack is compact/positional even though META is written named on disk.
    let m = MetaBlock::default();
    assert_eq!(to_msgpack(&m).unwrap()[0], 0x95);
}

// ------------------------------------------------------------------
// RingPayload
// ------------------------------------------------------------------

#[test]
fn ring_rt_empty() {
    let p = RingPayload::default();
    assert_eq!(rt(&p), p);
    assert!(!p.wrapped);
}

#[test]
fn ring_rt_codes_and_events() {
    let mut codes = HashMap::new();
    codes.insert(
        7u64,
        CodeInfo {
            file: "app.py".into(),
            qualname: "main".into(),
            first_line: 1,
        },
    );
    let p = RingPayload {
        codes,
        events: vec![
            Event::new(EventKind::PyStart, 0, 0, 7, 1),
            Event::new(EventKind::Line, 0, 2, 7, 2),
        ],
        wrapped: false,
    };
    assert_eq!(rt(&p), p);
}

#[test]
fn ring_rt_wrapped_true() {
    let p = RingPayload {
        codes: HashMap::new(),
        events: vec![Event::new(EventKind::Raise, 1, 5, 9, 100)],
        wrapped: true,
    };
    let back = rt(&p);
    assert!(back.wrapped);
    assert_eq!(back, p);
}

#[test]
fn ring_rt_many_events() {
    let events: Vec<Event> = (0..3000)
        .map(|i| Event::new(EventKind::Line, (i % 4) as u16, i, 1, i as u64))
        .collect();
    let p = RingPayload {
        codes: HashMap::new(),
        events,
        wrapped: true,
    };
    assert_eq!(rt(&p), p);
}

#[test]
fn ring_rt_multiple_codes() {
    let mut codes = HashMap::new();
    for i in 0..50u64 {
        codes.insert(
            i,
            CodeInfo {
                file: format!("mod{i}.py"),
                qualname: format!("fn{i}"),
                first_line: i as u32,
            },
        );
    }
    let p = RingPayload {
        codes,
        events: vec![Event::new(EventKind::PyReturn, 0, 0, 3, 7)],
        wrapped: false,
    };
    assert_eq!(rt(&p), p);
}

#[test]
fn ring_rt_big_code_ids() {
    let mut codes = HashMap::new();
    codes.insert(
        u64::MAX,
        CodeInfo {
            file: "x".into(),
            qualname: "y".into(),
            first_line: 1,
        },
    );
    let p = RingPayload {
        codes,
        events: vec![Event::new(EventKind::Line, 0, 1, u64::MAX, u64::MAX)],
        wrapped: false,
    };
    assert_eq!(rt(&p), p);
}

// ------------------------------------------------------------------
// MutationValue / Mutation
// ------------------------------------------------------------------

#[test]
fn mutationvalue_rt_full() {
    let v = MutationValue {
        kind: "dict".into(),
        repr: Some("{...}".into()),
        type_name: Some("collections.OrderedDict".into()),
        length: Some(3),
    };
    assert_eq!(rt(&v), v);
}

#[test]
fn mutationvalue_rt_all_none() {
    let v = MutationValue {
        kind: "none".into(),
        repr: None,
        type_name: None,
        length: None,
    };
    assert_eq!(rt(&v), v);
}

#[test]
fn mutationvalue_rt_max_length() {
    let v = MutationValue {
        kind: "list".into(),
        repr: None,
        type_name: None,
        length: Some(u64::MAX),
    };
    assert_eq!(rt(&v), v);
}

#[test]
fn mutation_rt_local() {
    let m = Mutation {
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
        frame: 140234,
    };
    assert_eq!(rt(&m), m);
}

#[test]
fn mutation_rt_item() {
    let m = Mutation {
        seq: 1,
        kind: "item".into(),
        name: "cache".into(),
        key: Some("user".into()),
        value: MutationValue {
            kind: "str".into(),
            repr: Some("bob".into()),
            type_name: None,
            length: Some(3),
        },
        file: "app.py".into(),
        qualname: "run".into(),
        line: 6,
        frame: 140234,
    };
    assert_eq!(rt(&m), m);
}

#[test]
fn mutation_rt_attr() {
    let m = Mutation {
        seq: 2,
        kind: "attr".into(),
        name: "self".into(),
        key: Some("count".into()),
        value: MutationValue {
            kind: "object".into(),
            repr: Some("<C>".into()),
            type_name: Some("app.C".into()),
            length: None,
        },
        file: "app.py".into(),
        qualname: "C.tick".into(),
        line: 12,
        frame: 999,
    };
    assert_eq!(rt(&m), m);
}

#[test]
fn mutation_rt_max_seq_and_frame() {
    let m = Mutation {
        seq: u64::MAX,
        kind: "local".into(),
        name: "x".into(),
        key: None,
        value: MutationValue {
            kind: "int".into(),
            repr: None,
            type_name: None,
            length: None,
        },
        file: "".into(),
        qualname: "".into(),
        line: u32::MAX,
        frame: u64::MAX,
    };
    assert_eq!(rt(&m), m);
}

#[test]
fn mutation_rt_unicode_names() {
    let m = Mutation {
        seq: 3,
        kind: "local".into(),
        name: "café".into(),
        key: Some("日本語".into()),
        value: MutationValue {
            kind: "str".into(),
            repr: Some("naïve 😀".into()),
            type_name: None,
            length: Some(6),
        },
        file: "модуль.py".into(),
        qualname: "функция".into(),
        line: 1,
        frame: 1,
    };
    assert_eq!(rt(&m), m);
}

#[test]
fn mutation_vec_rt() {
    let muts: Vec<Mutation> = (0..200)
        .map(|i| Mutation {
            seq: i,
            kind: "local".into(),
            name: format!("v{i}"),
            key: None,
            value: MutationValue {
                kind: "int".into(),
                repr: Some(i.to_string()),
                type_name: None,
                length: None,
            },
            file: "a.py".into(),
            qualname: "f".into(),
            line: i as u32,
            frame: 1,
        })
        .collect();
    assert_eq!(rt(&muts), muts);
}

// ------------------------------------------------------------------
// NonDetEvent
// ------------------------------------------------------------------

#[test]
fn nondet_rt_single() {
    let e = NonDetEvent {
        seq: 0,
        source: "time.time".into(),
        tag: "f".into(),
        payload: "1783000000.5".into(),
    };
    assert_eq!(rt(&e), e);
}

#[test]
fn nondet_rt_empty_payload() {
    let e = NonDetEvent {
        seq: 5,
        source: "os.urandom".into(),
        tag: "b".into(),
        payload: "".into(),
    };
    assert_eq!(rt(&e), e);
}

#[test]
fn nondet_rt_max_seq() {
    let e = NonDetEvent {
        seq: u64::MAX,
        source: "random.random".into(),
        tag: "f".into(),
        payload: "0.5".into(),
    };
    assert_eq!(rt(&e), e);
}

#[test]
fn nondet_rt_unicode_payload() {
    let e = NonDetEvent {
        seq: 1,
        source: "getenv".into(),
        tag: "s".into(),
        payload: "café=日本語😀".into(),
    };
    assert_eq!(rt(&e), e);
}

#[test]
fn nondet_vec_rt() {
    let events = vec![
        NonDetEvent {
            seq: 0,
            source: "time.time".into(),
            tag: "f".into(),
            payload: "1.0".into(),
        },
        NonDetEvent {
            seq: 1,
            source: "random.random".into(),
            tag: "f".into(),
            payload: "0.3".into(),
        },
        NonDetEvent {
            seq: 2,
            source: "os.urandom".into(),
            tag: "b".into(),
            payload: "deadbeef".into(),
        },
    ];
    assert_eq!(rt(&events), events);
}

#[test]
fn nondet_vec_large_rt() {
    let events: Vec<NonDetEvent> = (0..1000)
        .map(|i| NonDetEvent {
            seq: i,
            source: "time.time".into(),
            tag: "f".into(),
            payload: format!("{}.{}", i, i),
        })
        .collect();
    assert_eq!(rt(&events), events);
}

// ------------------------------------------------------------------
// ExceptionLink
// ------------------------------------------------------------------

#[test]
fn exclink_rt_head() {
    let e = ExceptionLink {
        exc_type: "ValueError".into(),
        message: "bad".into(),
        relation: "head".into(),
    };
    assert_eq!(rt(&e), e);
}

#[test]
fn exclink_rt_empty_message() {
    let e = ExceptionLink {
        exc_type: "StopIteration".into(),
        message: "".into(),
        relation: "cause".into(),
    };
    assert_eq!(rt(&e), e);
}

#[test]
fn exclink_rt_all_relations() {
    for rel in ["head", "cause", "context"] {
        let e = ExceptionLink {
            exc_type: "E".into(),
            message: "m".into(),
            relation: rel.into(),
        };
        assert_eq!(rt(&e).relation, rel);
    }
}

#[test]
fn exclink_chain_vec_rt() {
    let chain = vec![
        ExceptionLink {
            exc_type: "ValueError".into(),
            message: "bad".into(),
            relation: "head".into(),
        },
        ExceptionLink {
            exc_type: "KeyError".into(),
            message: "'k'".into(),
            relation: "context".into(),
        },
        ExceptionLink {
            exc_type: "RuntimeError".into(),
            message: "cause of it".into(),
            relation: "cause".into(),
        },
    ];
    assert_eq!(rt(&chain), chain);
}

#[test]
fn exclink_rt_unicode_message() {
    let e = ExceptionLink {
        exc_type: "ValueError".into(),
        message: "não pôde: café 😀".into(),
        relation: "head".into(),
    };
    assert_eq!(rt(&e), e);
}

// ------------------------------------------------------------------
// FrameInfo
// ------------------------------------------------------------------

#[test]
fn frame_rt_basic() {
    let f = FrameInfo {
        file: "app.py".into(),
        qualname: "main".into(),
        lineno: 3,
        first_lineno: 1,
        locals: vec![("x".into(), 0), ("cfg".into(), 1)],
    };
    assert_eq!(rt(&f), f);
}

#[test]
fn frame_rt_no_locals() {
    let f = FrameInfo {
        file: "a.py".into(),
        qualname: "f".into(),
        lineno: 1,
        first_lineno: 1,
        locals: vec![],
    };
    assert_eq!(rt(&f), f);
}

#[test]
fn frame_rt_many_locals() {
    let f = FrameInfo {
        file: "a.py".into(),
        qualname: "big".into(),
        lineno: 100,
        first_lineno: 1,
        locals: (0..300).map(|i| (format!("v{i}"), i as u64)).collect(),
    };
    assert_eq!(rt(&f), f);
}

#[test]
fn frame_rt_max_object_ids() {
    let f = FrameInfo {
        file: "a.py".into(),
        qualname: "f".into(),
        lineno: u32::MAX,
        first_lineno: u32::MAX,
        locals: vec![("big".into(), u64::MAX)],
    };
    assert_eq!(rt(&f), f);
}

#[test]
fn frame_rt_unicode_names() {
    let f = FrameInfo {
        file: "модуль.py".into(),
        qualname: "Класс.método".into(),
        lineno: 5,
        first_lineno: 2,
        locals: vec![("café".into(), 7), ("日本".into(), 8)],
    };
    assert_eq!(rt(&f), f);
}

#[test]
fn frame_vec_rt() {
    let frames: Vec<FrameInfo> = (0..50)
        .map(|i| FrameInfo {
            file: format!("f{i}.py"),
            qualname: format!("fn{i}"),
            lineno: i,
            first_lineno: 1,
            locals: vec![("x".into(), i as u64)],
        })
        .collect();
    assert_eq!(rt(&frames), frames);
}

// ------------------------------------------------------------------
// ObjectItem / ObjectNode
// ------------------------------------------------------------------

#[test]
fn objectitem_rt_key_some() {
    let it = ObjectItem {
        key: Some("k".into()),
        value_id: 42,
    };
    assert_eq!(rt(&it), it);
}

#[test]
fn objectitem_rt_key_none() {
    let it = ObjectItem {
        key: None,
        value_id: 7,
    };
    assert_eq!(rt(&it), it);
}

#[test]
fn objectitem_rt_max_id() {
    let it = ObjectItem {
        key: Some("last".into()),
        value_id: u64::MAX,
    };
    assert_eq!(rt(&it), it);
}

#[test]
fn objectnode_rt_scalar() {
    let n = ObjectNode {
        id: 0,
        kind: "int".into(),
        repr: Some("42".into()),
        type_name: None,
        length: None,
        truncated: false,
        items: vec![],
    };
    assert_eq!(rt(&n), n);
}

#[test]
fn objectnode_rt_container() {
    let n = ObjectNode {
        id: 1,
        kind: "dict".into(),
        repr: None,
        type_name: None,
        length: Some(2),
        truncated: false,
        items: vec![
            ObjectItem {
                key: Some("a".into()),
                value_id: 0,
            },
            ObjectItem {
                key: Some("b".into()),
                value_id: 2,
            },
        ],
    };
    assert_eq!(rt(&n), n);
}

#[test]
fn objectnode_rt_object_with_type_name() {
    let n = ObjectNode {
        id: 3,
        kind: "object".into(),
        repr: Some("<app.C object>".into()),
        type_name: Some("app.C".into()),
        length: None,
        truncated: false,
        items: vec![ObjectItem {
            key: Some("count".into()),
            value_id: 0,
        }],
    };
    assert_eq!(rt(&n), n);
}

#[test]
fn objectnode_rt_truncated_flag() {
    let n = ObjectNode {
        id: 5,
        kind: "list".into(),
        repr: None,
        type_name: None,
        length: Some(1_000_000),
        truncated: true,
        items: vec![],
    };
    let back = rt(&n);
    assert!(back.truncated);
    assert_eq!(back, n);
}

#[test]
fn objectnode_placeholder_rt() {
    let n = ObjectNode::placeholder(9);
    assert_eq!(n.id, 9);
    assert!(n.truncated);
    assert_eq!(n.kind, "truncated");
    assert_eq!(rt(&n), n);
}

#[test]
fn objectnode_rt_all_options_none() {
    let n = ObjectNode {
        id: 7,
        kind: "none".into(),
        repr: None,
        type_name: None,
        length: None,
        truncated: false,
        items: vec![],
    };
    assert_eq!(rt(&n), n);
}

#[test]
fn objectnode_rt_max_length() {
    let n = ObjectNode {
        id: u64::MAX,
        kind: "bytes".into(),
        repr: Some("b'...'".into()),
        type_name: None,
        length: Some(u64::MAX),
        truncated: true,
        items: vec![],
    };
    assert_eq!(rt(&n), n);
}

#[test]
fn objectnode_graph_vec_rt() {
    let graph = vec![
        ObjectNode {
            id: 0,
            kind: "int".into(),
            repr: Some("42".into()),
            type_name: None,
            length: None,
            truncated: false,
            items: vec![],
        },
        ObjectNode {
            id: 1,
            kind: "dict".into(),
            repr: None,
            type_name: None,
            length: Some(1),
            truncated: false,
            items: vec![ObjectItem {
                key: Some("k".into()),
                value_id: 0,
            }],
        },
    ];
    assert_eq!(rt(&graph), graph);
}

#[test]
fn objectnode_rt_unicode_repr() {
    let n = ObjectNode {
        id: 2,
        kind: "str".into(),
        repr: Some("café 日本語 😀".into()),
        type_name: None,
        length: Some(11),
        truncated: false,
        items: vec![],
    };
    assert_eq!(rt(&n), n);
}

// ------------------------------------------------------------------
// SourceFile
// ------------------------------------------------------------------

#[test]
fn source_rt_basic() {
    let s = SourceFile {
        filename: "app.py".into(),
        sha1: "abc123".into(),
        text: "x = 1\ny = 2\n".into(),
    };
    assert_eq!(rt(&s), s);
}

#[test]
fn source_rt_empty_text() {
    let s = SourceFile {
        filename: "empty.py".into(),
        sha1: "da39a3ee".into(),
        text: "".into(),
    };
    assert_eq!(rt(&s), s);
}

#[test]
fn source_rt_unicode_text() {
    let s = SourceFile {
        filename: "módulo.py".into(),
        sha1: "ff".into(),
        text: "# café ☕\nprint('日本語')\n".into(),
    };
    assert_eq!(rt(&s), s);
}

#[test]
fn source_rt_large_text() {
    let text: String = (0..5000).map(|i| format!("line_{i} = {i}\n")).collect();
    let s = SourceFile {
        filename: "big.py".into(),
        sha1: "deadbeef".into(),
        text,
    };
    assert_eq!(rt(&s), s);
}

#[test]
fn source_vec_rt() {
    let files: Vec<SourceFile> = (0..20)
        .map(|i| SourceFile {
            filename: format!("m{i}.py"),
            sha1: format!("{i:040x}"),
            text: format!("v = {i}\n"),
        })
        .collect();
    assert_eq!(rt(&files), files);
}
