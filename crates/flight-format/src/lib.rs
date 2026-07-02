//! The `.flight` file format, version 1.
//!
//! A `.flight` file is the black-box recording of a Python process ("a flight").
//! The format is the only contract between the recording engine and every
//! reader (viewer, tooling, future integrations), so it obeys three hard rules:
//!
//! 1. **Append-only.** A crashing process must be able to emit a useful file
//!    without ever seeking back. The footer (block index) is optional.
//! 2. **Tolerant to truncation.** Readers parse as far as the bytes allow and
//!    report the file as `partial` instead of failing.
//! 3. **Forward compatible.** Unknown block types are skipped, never errors.
//!    New readers read old files; old readers survive new files.
//!
//! Layout:
//!
//! ```text
//! [HEADER]   magic "FLGT" | u16 LE format version | u32 LE meta len | msgpack meta
//! [BLOCK]*   u8 block type | u32 LE payload len   | zstd(msgpack payload)
//! [FOOTER]   an INDEX block (0x70), then trailer: u32 LE index block total len | "TLGF"
//! ```
//!
//! The header meta is *uncompressed* msgpack so that tools can sniff a file
//! cheaply. Block payloads are msgpack compressed with zstd (level 3).

mod block;
mod crash;
mod error;
mod event;
mod header;
mod mutation;
mod writer;

pub use block::{BlockType, IndexEntry, MetaBlock, RingPayload};
pub use crash::{ExceptionLink, FrameInfo, ObjectItem, ObjectNode, SourceFile};
pub use error::FormatError;
pub use event::{CodeInfo, Event, EventKind};
pub use header::HeaderMeta;
pub use mutation::{Mutation, MutationValue};
pub use writer::FlightWriter;

/// File magic, first 4 bytes of every `.flight` file.
pub const MAGIC: &[u8; 4] = b"FLGT";
/// Trailer magic, last 4 bytes of a *cleanly closed* `.flight` file.
pub const TRAILER_MAGIC: &[u8; 4] = b"TLGF";
/// Current format version written by this crate.
pub const FORMAT_VERSION: u16 = 1;
/// zstd compression level for block payloads (speed/ratio sweet spot).
pub const ZSTD_LEVEL: i32 = 3;
/// Size in bytes of the fixed part of the header (before the meta bytes).
pub const HEADER_FIXED_LEN: usize = 4 + 2 + 4;
/// Size in bytes of a block header (type + payload length).
pub const BLOCK_HEADER_LEN: usize = 1 + 4;
/// Size in bytes of the trailer (index length + trailer magic).
pub const TRAILER_LEN: usize = 4 + 4;

/// Serialize a value to msgpack (structs become compact positional arrays).
pub fn to_msgpack<T: serde::Serialize>(value: &T) -> Result<Vec<u8>, FormatError> {
    rmp_serde::to_vec(value).map_err(|e| FormatError::Encode(e.to_string()))
}

/// Deserialize a value from msgpack.
pub fn from_msgpack<'a, T: serde::Deserialize<'a>>(bytes: &'a [u8]) -> Result<T, FormatError> {
    rmp_serde::from_slice(bytes).map_err(|e| FormatError::Decode(e.to_string()))
}

/// Compress a block payload.
pub fn compress(bytes: &[u8]) -> Result<Vec<u8>, FormatError> {
    zstd::encode_all(bytes, ZSTD_LEVEL).map_err(|e| FormatError::Encode(e.to_string()))
}

/// Decompress a block payload.
pub fn decompress(bytes: &[u8]) -> Result<Vec<u8>, FormatError> {
    zstd::decode_all(bytes).map_err(|e| FormatError::Decode(e.to_string()))
}
