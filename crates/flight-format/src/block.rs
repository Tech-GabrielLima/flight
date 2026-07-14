use std::collections::HashMap;

use serde::{Deserialize, Serialize};

use crate::event::{CodeInfo, Event};


#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[repr(u8)]
pub enum BlockType {

    Meta = 0x01,

    Source = 0x02,

    Exception = 0x03,

    Frame = 0x04,

    Object = 0x05,

    EventRing = 0x06,

    Mutation = 0x07,

    Timeline = 0x08,

    Nondet = 0x09,

    Index = 0x70,

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


#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize, Default)]
pub struct MetaBlock {
    pub python_version: String,
    pub platform: String,
    pub argv: Vec<String>,
    pub cwd: String,
    pub flight_version: String,
}


#[derive(Debug, Clone, PartialEq, Serialize, Deserialize, Default)]
pub struct RingPayload {

    pub codes: HashMap<u64, CodeInfo>,

    pub events: Vec<Event>,


    pub wrapped: bool,
}


#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub struct IndexEntry {

    pub block_type: u8,

    pub offset: u64,

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
