use std::path::Path;

use std::collections::HashMap;

use flight_format::{
    BlockType, ExceptionLink, FormatError, FrameInfo, HeaderMeta, IndexEntry, MetaBlock, Mutation,
    NonDetEvent, ObjectNode, RingPayload, SourceFile, BLOCK_HEADER_LEN, FORMAT_VERSION,
    HEADER_FIXED_LEN, MAGIC, TRAILER_LEN, TRAILER_MAGIC,
};

#[derive(Debug, Clone)]
pub struct RawBlock {
    pub block_type: u8,

    pub offset: u64,

    pub payload: Vec<u8>,
}

impl RawBlock {
    pub fn type_name(&self) -> &'static str {
        BlockType::from_u8(self.block_type)
            .map(|t| t.name())
            .unwrap_or("UNKNOWN")
    }
}

#[derive(Debug)]
pub struct FlightFile {
    pub format_version: u16,

    pub header: HeaderMeta,

    pub blocks: Vec<RawBlock>,

    pub partial: bool,

    pub used_index: bool,
}

impl FlightFile {
    pub fn open(path: &Path) -> Result<FlightFile, FormatError> {
        let bytes = std::fs::read(path)?;
        FlightFile::from_bytes(&bytes)
    }

    pub fn from_bytes(bytes: &[u8]) -> Result<FlightFile, FormatError> {
        let (format_version, header, body_start) = parse_header(bytes)?;

        let (blocks, partial, used_index) = match blocks_via_index(bytes, body_start) {
            Some(blocks) => (blocks, false, true),
            None => {
                let (blocks, clean) = scan_blocks(bytes, body_start);
                (blocks, !clean, false)
            }
        };

        Ok(FlightFile {
            format_version,
            header,
            blocks,
            partial,
            used_index,
        })
    }

    pub fn meta(&self) -> Option<MetaBlock> {
        self.first_payload(BlockType::Meta)
    }

    pub fn event_ring(&self) -> Option<RingPayload> {
        self.first_payload(BlockType::EventRing)
    }

    pub fn exceptions(&self) -> Vec<ExceptionLink> {
        self.first_payload(BlockType::Exception).unwrap_or_default()
    }

    pub fn frames(&self) -> Vec<FrameInfo> {
        self.first_payload(BlockType::Frame).unwrap_or_default()
    }

    pub fn sources(&self) -> Vec<SourceFile> {
        self.blocks
            .iter()
            .filter(|b| b.block_type == BlockType::Source as u8)
            .filter_map(|b| flight_format::from_msgpack::<Vec<SourceFile>>(&b.payload).ok())
            .flatten()
            .collect()
    }

    pub fn objects(&self) -> Vec<ObjectNode> {
        self.first_payload(BlockType::Object).unwrap_or_default()
    }

    pub fn mutations(&self) -> Vec<Mutation> {
        self.first_payload(BlockType::Mutation).unwrap_or_default()
    }

    pub fn nondet(&self) -> Vec<NonDetEvent> {
        self.first_payload(BlockType::Nondet).unwrap_or_default()
    }

    pub fn object_map(&self) -> HashMap<u64, ObjectNode> {
        self.objects().into_iter().map(|n| (n.id, n)).collect()
    }

    pub fn aliases(&self, object_id: u64) -> Vec<(usize, String)> {
        let mut out = Vec::new();
        for (fi, frame) in self.frames().iter().enumerate() {
            for (name, id) in &frame.locals {
                if *id == object_id {
                    out.push((fi, name.clone()));
                }
            }
        }
        out
    }

    fn first_payload<T: serde::de::DeserializeOwned>(&self, ty: BlockType) -> Option<T> {
        self.blocks
            .iter()
            .find(|b| b.block_type == ty as u8)
            .and_then(|b| flight_format::from_msgpack(&b.payload).ok())
    }
}

fn parse_header(bytes: &[u8]) -> Result<(u16, HeaderMeta, usize), FormatError> {
    if bytes.len() < HEADER_FIXED_LEN || &bytes[0..4] != MAGIC {
        return Err(FormatError::NotAFlightFile);
    }
    let version = u16::from_le_bytes([bytes[4], bytes[5]]);
    if version > FORMAT_VERSION {
        return Err(FormatError::UnsupportedVersion(version));
    }
    let meta_len = u32::from_le_bytes([bytes[6], bytes[7], bytes[8], bytes[9]]) as usize;
    let meta_end = HEADER_FIXED_LEN + meta_len;
    if bytes.len() < meta_end {
        return Err(FormatError::Decode("header truncated".to_string()));
    }
    let header: HeaderMeta = rmp_serde::from_slice(&bytes[HEADER_FIXED_LEN..meta_end])
        .map_err(|e| FormatError::Decode(e.to_string()))?;
    Ok((version, header, meta_end))
}

fn blocks_via_index(bytes: &[u8], body_start: usize) -> Option<Vec<RawBlock>> {
    if bytes.len() < TRAILER_LEN {
        return None;
    }
    let n = bytes.len();
    if &bytes[n - 4..] != TRAILER_MAGIC {
        return None;
    }
    let index_total_len =
        u32::from_le_bytes([bytes[n - 8], bytes[n - 7], bytes[n - 6], bytes[n - 5]]) as usize;
    let index_start = n.checked_sub(TRAILER_LEN + index_total_len)?;
    if index_start < body_start {
        return None;
    }
    let (ty, payload) = read_block_at(bytes, index_start)?;
    if ty != BlockType::Index as u8 {
        return None;
    }
    let entries: Vec<IndexEntry> = flight_format::from_msgpack(&payload).ok()?;

    let mut blocks = Vec::with_capacity(entries.len());
    for e in entries {
        let (ty, payload) = read_block_at(bytes, e.offset as usize)?;
        if ty != e.block_type {
            return None;
        }
        blocks.push(RawBlock {
            block_type: ty,
            offset: e.offset,
            payload,
        });
    }
    Some(blocks)
}

fn scan_blocks(bytes: &[u8], body_start: usize) -> (Vec<RawBlock>, bool) {
    let mut blocks = Vec::new();
    let mut pos = body_start;
    loop {
        if pos == bytes.len() {
            return (blocks, true);
        }

        if bytes.len() - pos == TRAILER_LEN && &bytes[bytes.len() - 4..] == TRAILER_MAGIC {
            return (blocks, true);
        }
        match read_block_at(bytes, pos) {
            Some((ty, payload)) => {
                let comp_len = u32::from_le_bytes([
                    bytes[pos + 1],
                    bytes[pos + 2],
                    bytes[pos + 3],
                    bytes[pos + 4],
                ]) as usize;

                if ty != BlockType::Index as u8 {
                    blocks.push(RawBlock {
                        block_type: ty,
                        offset: pos as u64,
                        payload,
                    });
                }
                pos += BLOCK_HEADER_LEN + comp_len;
            }
            None => return (blocks, false),
        }
    }
}

fn read_block_at(bytes: &[u8], pos: usize) -> Option<(u8, Vec<u8>)> {
    if bytes.len() < pos + BLOCK_HEADER_LEN {
        return None;
    }
    let ty = bytes[pos];
    let len = u32::from_le_bytes([
        bytes[pos + 1],
        bytes[pos + 2],
        bytes[pos + 3],
        bytes[pos + 4],
    ]) as usize;
    let start = pos + BLOCK_HEADER_LEN;
    let end = start.checked_add(len)?;
    if bytes.len() < end {
        return None;
    }
    let payload = flight_format::decompress(&bytes[start..end]).ok()?;
    Some((ty, payload))
}
