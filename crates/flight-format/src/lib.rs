mod block;
mod crash;
mod error;
mod event;
mod header;
mod mutation;
mod nondet;

#[cfg(feature = "c-zstd")]
mod writer;

pub use block::{BlockType, IndexEntry, MetaBlock, RingPayload};
pub use crash::{ExceptionLink, FrameInfo, ObjectItem, ObjectNode, SourceFile};
pub use error::FormatError;
pub use event::{CodeInfo, Event, EventKind};
pub use header::HeaderMeta;
pub use mutation::{Mutation, MutationValue};
pub use nondet::NonDetEvent;
#[cfg(feature = "c-zstd")]
pub use writer::FlightWriter;

pub const MAGIC: &[u8; 4] = b"FLGT";

pub const TRAILER_MAGIC: &[u8; 4] = b"TLGF";

pub const FORMAT_VERSION: u16 = 1;

pub const ZSTD_LEVEL: i32 = 3;

pub const HEADER_FIXED_LEN: usize = 4 + 2 + 4;

pub const BLOCK_HEADER_LEN: usize = 1 + 4;

pub const TRAILER_LEN: usize = 4 + 4;

pub fn to_msgpack<T: serde::Serialize>(value: &T) -> Result<Vec<u8>, FormatError> {
    rmp_serde::to_vec(value).map_err(|e| FormatError::Encode(e.to_string()))
}

pub fn from_msgpack<'a, T: serde::Deserialize<'a>>(bytes: &'a [u8]) -> Result<T, FormatError> {
    rmp_serde::from_slice(bytes).map_err(|e| FormatError::Decode(e.to_string()))
}

#[cfg(feature = "c-zstd")]
pub fn compress(bytes: &[u8]) -> Result<Vec<u8>, FormatError> {
    zstd::encode_all(bytes, ZSTD_LEVEL).map_err(|e| FormatError::Encode(e.to_string()))
}

#[cfg(feature = "c-zstd")]
pub fn decompress(bytes: &[u8]) -> Result<Vec<u8>, FormatError> {
    zstd::decode_all(bytes).map_err(|e| FormatError::Decode(e.to_string()))
}

#[cfg(all(not(feature = "c-zstd"), feature = "pure-zstd"))]
pub fn decompress(bytes: &[u8]) -> Result<Vec<u8>, FormatError> {
    use std::io::Read;
    let mut decoder = ruzstd::decoding::StreamingDecoder::new(bytes)
        .map_err(|e| FormatError::Decode(e.to_string()))?;
    let mut out = Vec::new();
    decoder
        .read_to_end(&mut out)
        .map_err(|e| FormatError::Decode(e.to_string()))?;
    Ok(out)
}
