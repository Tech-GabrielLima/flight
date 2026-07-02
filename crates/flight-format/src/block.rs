use std::collections::HashMap;

use serde::{Deserialize, Serialize};

use crate::event::{CodeInfo, Event};

/// Block types of format v1.
///
/// The numeric ids are part of the on-disk format — never renumber. Ids for
/// future phases are reserved *now* (P3: the format is the spine; the viewer
/// of phase 1.5 must gain phase-2 powers without a rewrite).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[repr(u8)]
pub enum BlockType {
    /// Environment: python version, platform, argv, cwd… (phase 0+)
    Meta = 0x01,
    /// Source code of the files involved in a crash. (phase 1)
    Source = 0x02,
    /// Exception chain: type, message, `__cause__`/`__context__`. (phase 1)
    Exception = 0x03,
    /// One stack frame: function, file, line, refs to locals. (phase 1)
    Frame = 0x04,
    /// Serialized object graph. (phase 1)
    Object = 0x05,
    /// The last N execution events before death. (phase 0+)
    EventRing = 0x06,
    /// One state write: who, what, new value, where, when. (phase 2)
    Mutation = 0x07,
    /// Checkpoints for efficient time navigation. (phase 2)
    Timeline = 0x08,
    /// Recorded sources of non-determinism. (phase 3)
    Nondet = 0x09,
    /// Footer: index of all previous blocks, written on clean close only.
    Index = 0x70,
    /// Extension space. Readers that don't know it: skip.
    Ext = 0x7F,
}

impl BlockType {
    pub fn from_u8(v: u8) -> Option<Self> {
        match v {
            0x01 => Some(BlockType::Meta),
            0x02 => Some(BlockType::Source),
            0x03 => Some(BlockType::Exception),
            0x04 => Some(BlockType::Frame),
            0x05 => Some(BlockType::Object),
            0x06 => Some(BlockType::EventRing),
            0x07 => Some(BlockType::Mutation),
            0x08 => Some(BlockType::Timeline),
            0x09 => Some(BlockType::Nondet),
            0x70 => Some(BlockType::Index),
            0x7F => Some(BlockType::Ext),
            _ => None,
        }
    }

    pub fn name(&self) -> &'static str {
        match self {
            BlockType::Meta => "META",
            BlockType::Source => "SOURCE",
            BlockType::Exception => "EXCEPTION",
            BlockType::Frame => "FRAME",
            BlockType::Object => "OBJECT",
            BlockType::EventRing => "EVENT_RING",
            BlockType::Mutation => "MUTATION",
            BlockType::Timeline => "TIMELINE",
            BlockType::Nondet => "NONDET",
            BlockType::Index => "INDEX",
            BlockType::Ext => "EXT",
        }
    }
}

/// Payload of a META block: the environment of the recorded process.
///
/// Serialized as a msgpack map (named fields) — this block is pure metadata
/// and must tolerate growing new fields between versions.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize, Default)]
pub struct MetaBlock {
    pub python_version: String,
    pub platform: String,
    pub argv: Vec<String>,
    pub cwd: String,
    pub flight_version: String,
}

/// Payload of an EVENT_RING block: the drained ring buffers of every thread,
/// merged and sorted by logical timestamp, plus the code map needed to
/// resolve `code_id`s into file/qualname.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize, Default)]
pub struct RingPayload {
    /// `code_id -> CodeInfo` for every code object seen in `events`.
    pub codes: HashMap<u64, CodeInfo>,
    /// Events oldest-first (ascending `tstamp`).
    pub events: Vec<Event>,
    /// True if any per-thread ring wrapped around, i.e. older events were
    /// overwritten and `events` is only the tail of the story.
    pub wrapped: bool,
}

/// One entry of the INDEX (footer) block.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub struct IndexEntry {
    /// Raw block type byte (kept raw so unknown types survive the index).
    pub block_type: u8,
    /// Absolute file offset of the block header.
    pub offset: u64,
    /// Compressed payload length in bytes.
    pub payload_len: u32,
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::event::EventKind;

    #[test]
    fn block_type_roundtrip_and_unknown() {
        for b in [
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
        ] {
            assert_eq!(BlockType::from_u8(b as u8), Some(b));
        }
        assert_eq!(BlockType::from_u8(0x42), None);
    }

    #[test]
    fn ring_payload_roundtrip() {
        let mut codes = HashMap::new();
        codes.insert(
            7u64,
            CodeInfo {
                file: "app.py".into(),
                qualname: "main".into(),
                first_line: 1,
            },
        );
        let payload = RingPayload {
            codes,
            events: vec![
                Event::new(EventKind::PyStart, 0, 0, 7, 1),
                Event::new(EventKind::Line, 0, 2, 7, 2),
            ],
            wrapped: false,
        };
        let bytes = crate::to_msgpack(&payload).unwrap();
        let back: RingPayload = crate::from_msgpack(&bytes).unwrap();
        assert_eq!(payload, back);
    }
}
