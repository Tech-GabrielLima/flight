package flight

import (
	"encoding/binary"
	"time"
)

func nowMillis() int64 { return time.Now().UnixMilli() }

// -- a tiny msgpack encoder (only the types the format uses) ---------------

func mpUint(n uint64) []byte {
	switch {
	case n < 0x80:
		return []byte{byte(n)}
	case n <= 0xff:
		return []byte{0xcc, byte(n)}
	case n <= 0xffff:
		b := []byte{0xcd, 0, 0}
		binary.BigEndian.PutUint16(b[1:], uint16(n))
		return b
	case n <= 0xffffffff:
		b := []byte{0xce, 0, 0, 0, 0}
		binary.BigEndian.PutUint32(b[1:], uint32(n))
		return b
	default:
		b := []byte{0xcf, 0, 0, 0, 0, 0, 0, 0, 0}
		binary.BigEndian.PutUint64(b[1:], n)
		return b
	}
}

func mpStr(s string) []byte {
	b := []byte(s)
	n := len(b)
	switch {
	case n < 32:
		return append([]byte{0xa0 | byte(n)}, b...)
	case n < 256:
		return append([]byte{0xd9, byte(n)}, b...)
	case n < 65536:
		h := []byte{0xda, 0, 0}
		binary.BigEndian.PutUint16(h[1:], uint16(n))
		return append(h, b...)
	default:
		h := []byte{0xdb, 0, 0, 0, 0}
		binary.BigEndian.PutUint32(h[1:], uint32(n))
		return append(h, b...)
	}
}

func mpBool(v bool) []byte {
	if v {
		return []byte{0xc3}
	}
	return []byte{0xc2}
}

func mpArray(items [][]byte) []byte {
	out := mpArrayHeader(len(items))
	for _, it := range items {
		out = append(out, it...)
	}
	return out
}

func mpArrayHeader(n int) []byte {
	switch {
	case n < 16:
		return []byte{0x90 | byte(n)}
	case n < 65536:
		h := []byte{0xdc, 0, 0}
		binary.BigEndian.PutUint16(h[1:], uint16(n))
		return h
	default:
		h := []byte{0xdd, 0, 0, 0, 0}
		binary.BigEndian.PutUint32(h[1:], uint32(n))
		return h
	}
}

func mpMap(pairs [][2][]byte) []byte {
	out := mpMapHeader(len(pairs))
	for _, kv := range pairs {
		out = append(out, kv[0]...)
		out = append(out, kv[1]...)
	}
	return out
}

func mpMapHeader(n int) []byte {
	switch {
	case n < 16:
		return []byte{0x80 | byte(n)}
	case n < 65536:
		h := []byte{0xde, 0, 0}
		binary.BigEndian.PutUint16(h[1:], uint16(n))
		return h
	default:
		h := []byte{0xdf, 0, 0, 0, 0}
		binary.BigEndian.PutUint32(h[1:], uint32(n))
		return h
	}
}

func mpStrArray(items []string) []byte {
	parts := make([][]byte, len(items))
	for i, s := range items {
		parts[i] = mpStr(s)
	}
	return mpArray(parts)
}

// -- a "stored" zstd frame: valid zstd, but with raw (uncompressed) blocks ---
//
// This lets any language emit a payload the Rust reader will `zstd::decode_all`
// without shipping a zstd encoder. Frame layout (RFC 8878): magic, a frame
// header declaring the content size (Single_Segment + 4-byte FCS), then one or
// more raw blocks; the last carries the Last_Block flag.
func zstdStored(data []byte) []byte {
	out := []byte{0x28, 0xb5, 0x2f, 0xfd} // frame magic
	out = append(out, 0xA0)               // descriptor: FCS_flag=2 (4B), Single_Segment=1
	var fcs [4]byte
	binary.LittleEndian.PutUint32(fcs[:], uint32(len(data)))
	out = append(out, fcs[:]...)

	const chunk = 65536 // <= 128 KiB block max; also <= window for our sizes
	if len(data) == 0 {
		out = append(out, blockHeader(0, true)...)
		return out
	}
	for i := 0; i < len(data); i += chunk {
		end := i + chunk
		if end > len(data) {
			end = len(data)
		}
		last := end >= len(data)
		out = append(out, blockHeader(end-i, last)...)
		out = append(out, data[i:end]...)
	}
	return out
}

// blockHeader builds a 3-byte little-endian zstd block header for a Raw_Block
// (block_type = 0): value = (size << 3) | (0 << 1) | last_block.
func blockHeader(size int, last bool) []byte {
	v := uint32(size) << 3
	if last {
		v |= 1
	}
	var b [4]byte
	binary.LittleEndian.PutUint32(b[:], v)
	return b[:3]
}
