use std::fs::File;
use std::io::{BufWriter, Write};
use std::path::Path;

use crate::block::{BlockType, IndexEntry};
use crate::error::FormatError;
use crate::header::HeaderMeta;
use crate::{FORMAT_VERSION, MAGIC, TRAILER_MAGIC};

/// Streaming, append-only writer of `.flight` files.
///
/// Every byte is written forward; the file on disk is valid (readable, maybe
/// `partial`) after *any* prefix of writes — the process may die at any
/// moment and rule 1 of the format still holds. Calling [`finish`] appends
/// the INDEX block and the trailer, marking a clean close; not calling it is
/// allowed and simply produces a footer-less file.
///
/// [`finish`]: FlightWriter::finish
pub struct FlightWriter<W: Write> {
    w: W,
    offset: u64,
    index: Vec<IndexEntry>,
}

impl FlightWriter<BufWriter<File>> {
    /// Create a `.flight` file at `path` and write its header.
    pub fn create(path: &Path, meta: &HeaderMeta) -> Result<Self, FormatError> {
        let file = File::create(path)?;
        FlightWriter::new(BufWriter::new(file), meta)
    }
}

impl<W: Write> FlightWriter<W> {
    /// Wrap `w` and immediately write the file header.
    pub fn new(mut w: W, meta: &HeaderMeta) -> Result<Self, FormatError> {
        // Header meta is a named msgpack map, uncompressed: sniffable and
        // tolerant to new fields.
        let meta_bytes =
            rmp_serde::to_vec_named(meta).map_err(|e| FormatError::Encode(e.to_string()))?;
        w.write_all(MAGIC)?;
        w.write_all(&FORMAT_VERSION.to_le_bytes())?;
        w.write_all(&(meta_bytes.len() as u32).to_le_bytes())?;
        w.write_all(&meta_bytes)?;
        let offset = (crate::HEADER_FIXED_LEN + meta_bytes.len()) as u64;
        Ok(FlightWriter {
            w,
            offset,
            index: Vec::new(),
        })
    }

    /// Write one block whose payload serializes as a compact positional
    /// msgpack array. Use for high-volume data (events, objects).
    pub fn write_block<T: serde::Serialize>(
        &mut self,
        ty: BlockType,
        payload: &T,
    ) -> Result<(), FormatError> {
        let bytes = crate::to_msgpack(payload)?;
        self.write_block_msgpack(ty as u8, &bytes)
    }

    /// Write one block whose payload serializes as a named msgpack map.
    /// Use for metadata-ish payloads that must tolerate growing new fields.
    pub fn write_block_named<T: serde::Serialize>(
        &mut self,
        ty: BlockType,
        payload: &T,
    ) -> Result<(), FormatError> {
        let bytes =
            rmp_serde::to_vec_named(payload).map_err(|e| FormatError::Encode(e.to_string()))?;
        self.write_block_msgpack(ty as u8, &bytes)
    }

    /// Write one block from already-encoded msgpack bytes. `ty` is raw so
    /// tests (and future tools) can emit types this crate does not know.
    pub fn write_block_msgpack(&mut self, ty: u8, msgpack: &[u8]) -> Result<(), FormatError> {
        let compressed = crate::compress(msgpack)?;
        self.index.push(IndexEntry {
            block_type: ty,
            offset: self.offset,
            payload_len: compressed.len() as u32,
        });
        self.w.write_all(&[ty])?;
        self.w.write_all(&(compressed.len() as u32).to_le_bytes())?;
        self.w.write_all(&compressed)?;
        self.offset += (crate::BLOCK_HEADER_LEN + compressed.len()) as u64;
        Ok(())
    }

    /// Flush buffered bytes to the underlying writer (the file stays valid,
    /// footer-less, if the process dies right after).
    pub fn flush(&mut self) -> Result<(), FormatError> {
        self.w.flush()?;
        Ok(())
    }

    /// Clean close: append the INDEX block and the fixed-size trailer, then
    /// flush. Consumes the writer.
    pub fn finish(mut self) -> Result<W, FormatError> {
        let index_bytes = crate::to_msgpack(&self.index)?;
        let index_start = self.offset;
        // The index is itself a regular block (a scanning reader just sees
        // one more block); the trailer lets a seeking reader jump to it.
        self.write_block_msgpack(BlockType::Index as u8, &index_bytes)?;
        let index_total_len = (self.offset - index_start) as u32;
        self.w.write_all(&index_total_len.to_le_bytes())?;
        self.w.write_all(TRAILER_MAGIC)?;
        self.w.flush()?;
        Ok(self.w)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::block::MetaBlock;

    #[test]
    fn header_layout_is_exact() {
        let meta = HeaderMeta {
            tool: "flight".into(),
            flight_version: "0.0.1".into(),
            created_unix_ms: 1,
        };
        let mut buf = Vec::new();
        let w = FlightWriter::new(&mut buf, &meta).unwrap();
        drop(w);
        assert_eq!(&buf[0..4], MAGIC);
        assert_eq!(u16::from_le_bytes([buf[4], buf[5]]), FORMAT_VERSION);
        let meta_len = u32::from_le_bytes([buf[6], buf[7], buf[8], buf[9]]) as usize;
        assert_eq!(buf.len(), crate::HEADER_FIXED_LEN + meta_len);
        let back: HeaderMeta = rmp_serde::from_slice(&buf[10..]).unwrap();
        assert_eq!(back, meta);
    }

    #[test]
    fn finish_appends_index_and_trailer() {
        let meta = HeaderMeta::new("0.0.1");
        let mut buf = Vec::new();
        let mut w = FlightWriter::new(&mut buf, &meta).unwrap();
        w.write_block_named(BlockType::Meta, &MetaBlock::default())
            .unwrap();
        let _ = w.finish().unwrap();
        assert_eq!(&buf[buf.len() - 4..], TRAILER_MAGIC);
        let n = buf.len();
        let index_total_len =
            u32::from_le_bytes([buf[n - 8], buf[n - 7], buf[n - 6], buf[n - 5]]) as usize;
        // Trailer points exactly at the start of the INDEX block.
        let index_start = n - crate::TRAILER_LEN - index_total_len;
        assert_eq!(buf[index_start], BlockType::Index as u8);
    }

    #[test]
    fn file_without_finish_has_no_trailer_but_is_intact() {
        let meta = HeaderMeta::new("0.0.1");
        let mut buf = Vec::new();
        let mut w = FlightWriter::new(&mut buf, &meta).unwrap();
        w.write_block_named(BlockType::Meta, &MetaBlock::default())
            .unwrap();
        w.flush().unwrap();
        drop(w); // process "dies" here
        assert_ne!(&buf[buf.len() - 4..], TRAILER_MAGIC);
    }
}
