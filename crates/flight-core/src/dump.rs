use std::path::Path;

use flight_format::{
    BlockType, ExceptionLink, FlightWriter, FormatError, FrameInfo, HeaderMeta, MetaBlock,
    ObjectNode, SourceFile,
};

use crate::recorder::Recorder;

/// Write a complete `.flight` file from the recorder's current state.
///
/// Phase 0/1 black-box shape: a META block (environment) followed by an
/// EVENT_RING block (the merged rear-view mirror). The file is cleanly closed
/// with a footer index, because a deliberate `dump()`/`capture()` is a clean
/// exit — the crash path that skips `finish()` is exercised by the writer's
/// own tests and by the reader's truncation suite.
///
/// Follows P1 (never take down the user's process): the whole thing is a
/// single fallible call the caller wraps; on any error the partially written
/// file is still a valid, `partial` `.flight` per the format's rules.
pub fn dump(path: &Path, meta: MetaBlock, recorder: &Recorder) -> Result<(), FormatError> {
    let header = HeaderMeta::new(&meta.flight_version);
    let mut w = FlightWriter::create(path, &header)?;
    w.write_block_named(BlockType::Meta, &meta)?;
    let ring = recorder.snapshot_ring();
    w.write_block(BlockType::EventRing, &ring)?;
    w.finish()?;
    Ok(())
}

/// The full Phase-1 black box: META, one SOURCE block per file, the EXCEPTION
/// chain, the FRAME stack, the OBJECT graph, and the EVENT_RING.
///
/// The Python engine has already done the dangerous work (walking live objects
/// under a time/byte budget); this just lays the blocks down in order and
/// closes the file. Written crash-first so that if the reader is ever truncated
/// mid-file, the most relevant frames/objects survive (VISION.md §1.3).
pub fn dump_crash(
    path: &Path,
    meta: MetaBlock,
    sources: Vec<SourceFile>,
    exceptions: Vec<ExceptionLink>,
    frames: Vec<FrameInfo>,
    objects: Vec<ObjectNode>,
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
    // One SOURCE block per file keeps each self-contained and lets a truncated
    // reader still use the files it did get.
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
            rec.record(EventKind::Line, 1, 1, 10 + i);
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
