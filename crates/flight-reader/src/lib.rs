//! Reader for `.flight` files.
//!
//! Reading strategy, in order:
//!
//! 1. **Trailer path.** If the file ends with a valid trailer, jump straight
//!    to the INDEX block and use it (fast path for cleanly closed files).
//! 2. **Linear scan.** Otherwise — the process died, the file was truncated,
//!    or the trailer is corrupt — scan blocks from the start and take
//!    everything that parses. The result is flagged [`FlightFile::partial`].
//!
//! Two tolerance rules are absolute:
//! - a block with an **unknown type** is kept as raw bytes and skipped by
//!   typed accessors — never an error (old readers must survive new files);
//! - a **truncated or corrupt tail** ends the scan gracefully — everything
//!   before it is served normally.

use std::path::Path;

use flight_format::{
    BlockType, FormatError, HeaderMeta, IndexEntry, MetaBlock, RingPayload, BLOCK_HEADER_LEN,
    FORMAT_VERSION, HEADER_FIXED_LEN, MAGIC, TRAILER_LEN, TRAILER_MAGIC,
};

/// One block as found in the file, payload already decompressed.
#[derive(Debug, Clone)]
pub struct RawBlock {
    /// Raw type byte (may be a type this reader does not know).
    pub block_type: u8,
    /// Absolute offset of the block header in the file.
    pub offset: u64,
    /// Decompressed msgpack payload.
    pub payload: Vec<u8>,
}

impl RawBlock {
    pub fn type_name(&self) -> &'static str {
        BlockType::from_u8(self.block_type)
            .map(|t| t.name())
            .unwrap_or("UNKNOWN")
    }
}

/// A parsed `.flight` file.
#[derive(Debug)]
pub struct FlightFile {
    /// Format version declared in the header.
    pub format_version: u16,
    /// Header metadata (who wrote the file, when).
    pub header: HeaderMeta,
    /// All blocks that parsed, in file order (INDEX block excluded).
    pub blocks: Vec<RawBlock>,
    /// True if any part of the file failed to parse (truncation, corrupt
    /// payload, missing footer on a file that has one more partial block…).
    pub partial: bool,
    /// True if the footer index was present, valid and used.
    pub used_index: bool,
}

impl FlightFile {
    /// Open and fully parse a `.flight` file.
    pub fn open(path: &Path) -> Result<FlightFile, FormatError> {
        let bytes = std::fs::read(path)?;
        FlightFile::from_bytes(&bytes)
    }

    /// Parse a `.flight` file from memory.
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

    /// The first META block, decoded — or `None` if the file has none (e.g.
    /// truncated before it was written).
    pub fn meta(&self) -> Option<MetaBlock> {
        self.first_payload(BlockType::Meta)
    }

    /// The first EVENT_RING block, decoded.
    pub fn event_ring(&self) -> Option<RingPayload> {
        self.first_payload(BlockType::EventRing)
    }

    fn first_payload<T: serde::de::DeserializeOwned>(&self, ty: BlockType) -> Option<T> {
        self.blocks
            .iter()
            .find(|b| b.block_type == ty as u8)
            .and_then(|b| flight_format::from_msgpack(&b.payload).ok())
    }
}

/// Parse the fixed header. This is the only place `open` can hard-fail:
/// without a magic and a version there is nothing to be tolerant *about*.
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

/// Fast path: locate the INDEX block through the trailer and load blocks by
/// offset. Any inconsistency returns `None` and the caller falls back to the
/// linear scan — the index is an optimization, never a requirement.
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
            return None; // index disagrees with the bytes: fall back to scan
        }
        blocks.push(RawBlock {
            block_type: ty,
            offset: e.offset,
            payload,
        });
    }
    Some(blocks)
}

/// Slow path: walk blocks from `body_start` until the bytes run out or stop
/// making sense. Returns the parsed blocks and whether the walk was clean
/// (ended exactly at EOF or at a valid trailer).
fn scan_blocks(bytes: &[u8], body_start: usize) -> (Vec<RawBlock>, bool) {
    let mut blocks = Vec::new();
    let mut pos = body_start;
    loop {
        if pos == bytes.len() {
            return (blocks, true); // clean end: footer-less but whole file
        }
        // A valid trailer also ends the walk cleanly (we just reached the
        // INDEX block, which is a footer and never a content block).
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
                // The INDEX block is a footer, not content — never surface it,
                // on any end path (clean EOF, trailer, or truncation).
                if ty != BlockType::Index as u8 {
                    blocks.push(RawBlock {
                        block_type: ty,
                        offset: pos as u64,
                        payload,
                    });
                }
                pos += BLOCK_HEADER_LEN + comp_len;
            }
            None => return (blocks, false), // truncated/corrupt tail: keep what we have
        }
    }
}

/// Read and decompress one block at `pos`. `None` on truncation or corrupt
/// payload.
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
