use std::path::Path;

use flight_format::{
    BlockType, ExceptionLink, FlightWriter, FormatError, FrameInfo, HeaderMeta, MetaBlock,
    Mutation, NonDetEvent, ObjectNode, SourceFile,
};

use crate::recorder::Recorder;

pub fn dump(path: &Path, meta: MetaBlock, recorder: &Recorder) -> Result<(), FormatError> {
    let header = HeaderMeta::new(&meta.flight_version);
    let mut w = FlightWriter::create(path, &header)?;
    w.write_block_named(BlockType::Meta, &meta)?;
    let ring = recorder.snapshot_ring();
    w.write_block(BlockType::EventRing, &ring)?;
    w.finish()?;
    Ok(())
}

#[allow(clippy::too_many_arguments)]
pub fn dump_crash(
    path: &Path,
    meta: MetaBlock,
    sources: Vec<SourceFile>,
    exceptions: Vec<ExceptionLink>,
    frames: Vec<FrameInfo>,
    objects: Vec<ObjectNode>,
    nondet: Vec<NonDetEvent>,
    recorder: &Recorder,
) -> Result<(), FormatError> {
    let header = HeaderMeta::new(&meta.flight_version);
    let mut w = FlightWriter::create(path, &header)?;
    w.write_block_named(BlockType::Meta, &meta)?;
    if !exceptions.is_empty() {
        w.write_block(BlockType::Exception, &exceptions)?;
    }
    if !frames.is_empty() {
        w.write_block(BlockType::Frame, &frames)?;
    }
    if !objects.is_empty() {
        w.write_block(BlockType::Object, &objects)?;
    }
    if !nondet.is_empty() {
        w.write_block(BlockType::Nondet, &nondet)?;
    }

    for src in sources {
        w.write_block(BlockType::Source, &vec![src])?;
    }
    let ring = recorder.snapshot_ring();
    w.write_block(BlockType::EventRing, &ring)?;
    w.finish()?;
    Ok(())
}

pub fn dump_nondet(
    path: &Path,
    meta: MetaBlock,
    events: Vec<NonDetEvent>,
    sources: Vec<SourceFile>,
    recorder: &Recorder,
) -> Result<(), FormatError> {
    let header = HeaderMeta::new(&meta.flight_version);
    let mut w = FlightWriter::create(path, &header)?;
    w.write_block_named(BlockType::Meta, &meta)?;
    if !events.is_empty() {
        w.write_block(BlockType::Nondet, &events)?;
    }
    for src in sources {
        w.write_block(BlockType::Source, &vec![src])?;
    }
    let ring = recorder.snapshot_ring();
    w.write_block(BlockType::EventRing, &ring)?;
    w.finish()?;
    Ok(())
}

pub fn dump_scope(
    path: &Path,
    meta: MetaBlock,
    mutations: Vec<Mutation>,
    sources: Vec<SourceFile>,
    recorder: &Recorder,
) -> Result<(), FormatError> {
    let header = HeaderMeta::new(&meta.flight_version);
    let mut w = FlightWriter::create(path, &header)?;
    w.write_block_named(BlockType::Meta, &meta)?;
    if !mutations.is_empty() {
        w.write_block(BlockType::Mutation, &mutations)?;
    }
    for src in sources {
        w.write_block(BlockType::Source, &vec![src])?;
    }
    let ring = recorder.snapshot_ring();
    w.write_block(BlockType::EventRing, &ring)?;
    w.finish()?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use flight_format::EventKind;
    use flight_reader::FlightFile;

    fn tmp(name: &str) -> std::path::PathBuf {
        let mut p = std::env::temp_dir();
        p.push(format!(
            "flight-core-test-{}-{}.flight",
            std::process::id(),
            name
        ));
        p
    }

    #[test]
    fn dump_produces_a_readable_file() {
        let rec = Recorder::new(1024);
        rec.register_code(1, "app.py", "main", 1);
        for i in 0..20 {
            rec.record(EventKind::Line, 1, 10 + i);
        }
        let meta = MetaBlock {
            python_version: "3.13.1".into(),
            platform: "test".into(),
            argv: vec!["app.py".into()],
            cwd: "/tmp".into(),
            flight_version: "0.0.1".into(),
        };
        let path = tmp("dump");
        dump(&path, meta.clone(), &rec).unwrap();

        let f = FlightFile::open(&path).unwrap();
        assert!(!f.partial);
        assert!(f.used_index);
        assert_eq!(f.meta().unwrap(), meta);
        let ring = f.event_ring().unwrap();
        assert_eq!(ring.events.len(), 20);
        assert_eq!(ring.codes[&1].qualname, "main");

        std::fs::remove_file(&path).ok();
    }
}

#[cfg(test)]
mod dump_ext {
    use super::*;
    use flight_format::{
        BlockType, EventKind, ExceptionLink, FrameInfo, MutationValue, ObjectItem, ObjectNode,
    };
    use flight_reader::FlightFile;
    use std::sync::atomic::{AtomicU64, Ordering};

    static COUNTER: AtomicU64 = AtomicU64::new(0);

    fn tmp(name: &str) -> std::path::PathBuf {
        let n = COUNTER.fetch_add(1, Ordering::Relaxed);
        let mut p = std::env::temp_dir();
        p.push(format!(
            "flight-core-ext-{}-{}-{}.flight",
            std::process::id(),
            name,
            n
        ));
        p
    }

    struct Cleanup(std::path::PathBuf);
    impl Drop for Cleanup {
        fn drop(&mut self) {
            std::fs::remove_file(&self.0).ok();
        }
    }

    fn meta() -> MetaBlock {
        MetaBlock {
            python_version: "3.13.1".into(),
            platform: "test".into(),
            argv: vec!["app.py".into()],
            cwd: "/tmp".into(),
            flight_version: "0.0.1".into(),
        }
    }

    fn recorder_with(n: u32) -> Recorder {
        let rec = Recorder::new(1024);
        rec.register_code(1, "app.py", "main", 1);
        for i in 0..n {
            rec.record(EventKind::Line, 1, 10 + i);
        }
        rec
    }

    fn src(name: &str) -> SourceFile {
        SourceFile {
            filename: name.into(),
            sha1: "h".into(),
            text: "x=1\n".into(),
        }
    }

    #[test]
    fn dump_is_clean_and_indexed() {
        let path = tmp("dump-clean");
        let _c = Cleanup(path.clone());
        dump(&path, meta(), &recorder_with(20)).unwrap();
        let f = FlightFile::open(&path).unwrap();
        assert!(!f.partial);
        assert!(f.used_index);
    }

    #[test]
    fn dump_meta_and_ring_roundtrip() {
        let path = tmp("dump-rt");
        let _c = Cleanup(path.clone());
        dump(&path, meta(), &recorder_with(20)).unwrap();
        let f = FlightFile::open(&path).unwrap();
        assert_eq!(f.meta().unwrap(), meta());
        let ring = f.event_ring().unwrap();
        assert_eq!(ring.events.len(), 20);
        assert_eq!(ring.codes[&1].qualname, "main");
    }

    #[test]
    fn dump_has_exactly_meta_and_ring_blocks() {
        let path = tmp("dump-blocks");
        let _c = Cleanup(path.clone());
        dump(&path, meta(), &recorder_with(5)).unwrap();
        let f = FlightFile::open(&path).unwrap();
        let types: Vec<u8> = f.blocks.iter().map(|b| b.block_type).collect();
        assert_eq!(
            types,
            vec![BlockType::Meta as u8, BlockType::EventRing as u8]
        );
    }

    #[test]
    fn dump_with_empty_recorder_still_readable() {
        let path = tmp("dump-empty");
        let _c = Cleanup(path.clone());
        dump(&path, meta(), &Recorder::new(64)).unwrap();
        let f = FlightFile::open(&path).unwrap();
        assert_eq!(f.meta().unwrap(), meta());
        assert_eq!(f.event_ring().unwrap().events.len(), 0);
    }

    #[test]
    fn dump_drains_the_ring() {
        let rec = recorder_with(10);
        let p1 = tmp("dump-drain-1");
        let p2 = tmp("dump-drain-2");
        let _c1 = Cleanup(p1.clone());
        let _c2 = Cleanup(p2.clone());
        dump(&p1, meta(), &rec).unwrap();
        dump(&p2, meta(), &rec).unwrap();
        let a = FlightFile::open(&p1).unwrap().event_ring().unwrap();
        let b = FlightFile::open(&p2).unwrap().event_ring().unwrap();
        assert_eq!(a.events.len(), b.events.len());
    }

    fn exceptions() -> Vec<ExceptionLink> {
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

    fn frames() -> Vec<FrameInfo> {
        vec![FrameInfo {
            file: "app.py".into(),
            qualname: "boom".into(),
            lineno: 3,
            first_lineno: 1,
            locals: vec![("cfg".into(), 7)],
        }]
    }

    fn objects() -> Vec<ObjectNode> {
        vec![ObjectNode {
            id: 7,
            kind: "dict".into(),
            repr: None,
            type_name: None,
            length: Some(1),
            truncated: false,
            items: vec![ObjectItem {
                key: Some("k".into()),
                value_id: 7,
            }],
        }]
    }

    fn nondet() -> Vec<NonDetEvent> {
        vec![NonDetEvent {
            seq: 0,
            source: "time.time".into(),
            tag: "f".into(),
            payload: "1.5".into(),
        }]
    }

    #[test]
    fn dump_crash_full_roundtrip() {
        let path = tmp("crash-full");
        let _c = Cleanup(path.clone());
        dump_crash(
            &path,
            meta(),
            vec![src("app.py"), src("lib.py")],
            exceptions(),
            frames(),
            objects(),
            nondet(),
            &recorder_with(15),
        )
        .unwrap();
        let f = FlightFile::open(&path).unwrap();
        assert!(!f.partial);
        assert_eq!(f.meta().unwrap(), meta());
        assert_eq!(f.exceptions().len(), 2);
        assert_eq!(f.frames(), frames());
        assert_eq!(f.objects(), objects());
        assert_eq!(f.nondet(), nondet());
        assert_eq!(f.sources().len(), 2);
        assert_eq!(f.event_ring().unwrap().events.len(), 15);
    }

    #[test]
    fn dump_crash_aliasing_resolves_through_reader() {
        let path = tmp("crash-alias");
        let _c = Cleanup(path.clone());
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
                locals: vec![("config".into(), 7)],
            },
        ];
        dump_crash(
            &path,
            meta(),
            vec![],
            exceptions(),
            frames,
            objects(),
            vec![],
            &Recorder::new(64),
        )
        .unwrap();
        let f = FlightFile::open(&path).unwrap();
        assert_eq!(
            f.aliases(7),
            vec![(0, "cfg".to_string()), (1, "config".to_string())]
        );
    }

    #[test]
    fn dump_crash_skips_empty_optional_blocks() {
        let path = tmp("crash-minimal");
        let _c = Cleanup(path.clone());

        dump_crash(
            &path,
            meta(),
            vec![],
            vec![],
            vec![],
            vec![],
            vec![],
            &recorder_with(3),
        )
        .unwrap();
        let f = FlightFile::open(&path).unwrap();
        let types: Vec<u8> = f.blocks.iter().map(|b| b.block_type).collect();
        assert_eq!(
            types,
            vec![BlockType::Meta as u8, BlockType::EventRing as u8]
        );
        assert!(f.exceptions().is_empty());
        assert!(f.frames().is_empty());
        assert!(f.objects().is_empty());
        assert!(f.nondet().is_empty());
        assert!(f.sources().is_empty());
    }

    #[test]
    fn dump_crash_one_source_block_per_file() {
        let path = tmp("crash-sources");
        let _c = Cleanup(path.clone());
        dump_crash(
            &path,
            meta(),
            vec![src("a.py"), src("b.py"), src("c.py")],
            vec![],
            vec![],
            vec![],
            vec![],
            &Recorder::new(64),
        )
        .unwrap();
        let f = FlightFile::open(&path).unwrap();
        let source_blocks = f
            .blocks
            .iter()
            .filter(|b| b.block_type == BlockType::Source as u8)
            .count();
        assert_eq!(source_blocks, 3, "one SOURCE block per file");
        assert_eq!(f.sources().len(), 3);
    }

    #[test]
    fn dump_crash_is_written_crash_first() {
        let path = tmp("crash-order");
        let _c = Cleanup(path.clone());
        dump_crash(
            &path,
            meta(),
            vec![src("a.py")],
            exceptions(),
            frames(),
            objects(),
            nondet(),
            &recorder_with(5),
        )
        .unwrap();
        let f = FlightFile::open(&path).unwrap();
        let types: Vec<u8> = f.blocks.iter().map(|b| b.block_type).collect();
        let ring_pos = types
            .iter()
            .position(|&t| t == BlockType::EventRing as u8)
            .unwrap();
        let exc_pos = types
            .iter()
            .position(|&t| t == BlockType::Exception as u8)
            .unwrap();
        assert!(exc_pos < ring_pos, "exception block precedes the ring");
    }

    #[test]
    fn dump_nondet_roundtrip() {
        let path = tmp("nondet");
        let _c = Cleanup(path.clone());
        let events = vec![
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
        ];
        dump_nondet(
            &path,
            meta(),
            events.clone(),
            vec![src("a.py")],
            &recorder_with(4),
        )
        .unwrap();
        let f = FlightFile::open(&path).unwrap();
        assert!(!f.partial);
        assert_eq!(f.nondet(), events);
        assert_eq!(f.sources().len(), 1);
        assert_eq!(f.event_ring().unwrap().events.len(), 4);
    }

    #[test]
    fn dump_nondet_empty_tape_skips_the_block() {
        let path = tmp("nondet-empty");
        let _c = Cleanup(path.clone());
        dump_nondet(&path, meta(), vec![], vec![], &recorder_with(2)).unwrap();
        let f = FlightFile::open(&path).unwrap();
        assert!(f.nondet().is_empty());
        assert!(f
            .blocks
            .iter()
            .all(|b| b.block_type != BlockType::Nondet as u8));
    }

    fn mutations() -> Vec<Mutation> {
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
                frame: 1,
            },
        ]
    }

    #[test]
    fn dump_scope_roundtrip() {
        let path = tmp("scope");
        let _c = Cleanup(path.clone());
        dump_scope(
            &path,
            meta(),
            mutations(),
            vec![src("a.py")],
            &recorder_with(6),
        )
        .unwrap();
        let f = FlightFile::open(&path).unwrap();
        assert!(!f.partial);
        assert_eq!(f.mutations(), mutations());
        assert_eq!(f.sources().len(), 1);
        assert_eq!(f.event_ring().unwrap().events.len(), 6);
    }

    #[test]
    fn dump_scope_empty_mutations_skips_the_block() {
        let path = tmp("scope-empty");
        let _c = Cleanup(path.clone());
        dump_scope(&path, meta(), vec![], vec![], &recorder_with(1)).unwrap();
        let f = FlightFile::open(&path).unwrap();
        assert!(f.mutations().is_empty());
        assert!(f
            .blocks
            .iter()
            .all(|b| b.block_type != BlockType::Mutation as u8));
    }

    #[test]
    fn dump_scope_mutation_history_preserved() {
        let path = tmp("scope-hist");
        let _c = Cleanup(path.clone());
        dump_scope(&path, meta(), mutations(), vec![], &Recorder::new(64)).unwrap();
        let f = FlightFile::open(&path).unwrap();
        let read = f.mutations();
        assert_eq!(read[0].kind, "local");
        assert_eq!(read[1].kind, "item");
        assert_eq!(read[1].key.as_deref(), Some("user"));
    }

    #[test]
    fn all_dump_variants_produce_indexed_files() {
        for name in ["v1", "v2", "v3", "v4"] {
            let path = tmp(name);
            let _c = Cleanup(path.clone());
            match name {
                "v1" => dump(&path, meta(), &recorder_with(2)).unwrap(),
                "v2" => dump_crash(
                    &path,
                    meta(),
                    vec![],
                    exceptions(),
                    frames(),
                    objects(),
                    nondet(),
                    &recorder_with(2),
                )
                .unwrap(),
                "v3" => dump_nondet(&path, meta(), nondet(), vec![], &recorder_with(2)).unwrap(),
                _ => dump_scope(&path, meta(), mutations(), vec![], &recorder_with(2)).unwrap(),
            }
            let f = FlightFile::open(&path).unwrap();
            assert!(f.used_index);
            assert!(!f.partial);
        }
    }
}
