// Package flight writes the SAME `.flight` black-box format the Python/Rust
// engine reads — from Go, with zero dependencies.
//
// The format is deliberately language-agnostic (VISION.md): a header, a series
// of blocks whose payloads are msgpack compressed with zstd, and an optional
// footer index. A recorder in any language that can produce those bytes writes
// a `.flight` that `flight inspect`, the TUI viewer and every other tool read
// unchanged — which is the whole point: one black-box format across a
// polyglot system, and a cross-language object graph.
//
// Two things usually pull in dependencies — msgpack and zstd — and this package
// avoids both:
//
//   - a tiny msgpack encoder covers exactly the types the format uses (uint,
//     string, array, map, bool);
//   - zstd payloads are written as a **stored frame**: a valid zstd frame whose
//     blocks are raw (uncompressed). The Rust reader decompresses it like any
//     other zstd stream; we just never invoke a compressor.
//
// So `go build` needs nothing but the standard library.
package flight

import (
	"encoding/binary"
	"os"
	"runtime"
)

// Event kinds — the on-disk numeric values shared with the Python engine
// (event.rs). A Go recorder maps its own notion of "a function started / a line
// ran / an error was raised" onto these.
const (
	KindStart   uint8 = 1 // a function started
	KindReturn  uint8 = 2 // a function returned
	KindLine    uint8 = 3 // a source line ran
	KindRaise   uint8 = 4 // an error was raised
	KindReraise uint8 = 5
	KindUnwind  uint8 = 6
)

const (
	blockMeta      byte = 0x01
	blockEventRing byte = 0x06
	blockIndex     byte = 0x70
	formatVersion  uint16 = 1
)

var magic = []byte("FLGT")
var trailer = []byte("TLGF")

// CodeInfo resolves a code id to something readable (file/qualname/first line).
type CodeInfo struct {
	File      string
	Qualname  string
	FirstLine uint32
}

type event struct {
	kind   uint8
	thread uint16
	line   uint32
	codeID uint64
	tstamp uint64
}

// Meta is the recorded process's environment, mirroring the Python META block.
// For a Go recorder, PythonVersion carries the Go runtime version and Platform
// the GOOS/GOARCH — the fields are just strings the reader displays.
type Meta struct {
	PythonVersion string
	Platform      string
	Argv          []string
	Cwd           string
	FlightVersion string
}

// DefaultMeta fills Meta from the Go runtime, so callers usually pass this.
func DefaultMeta() Meta {
	cwd, _ := os.Getwd()
	return Meta{
		PythonVersion: runtime.Version(),
		Platform:      runtime.GOOS + "/" + runtime.GOARCH,
		Argv:          os.Args,
		Cwd:           cwd,
		FlightVersion: "0.0.1-go",
	}
}

// Recorder accumulates events and their code map, then writes a `.flight`.
type Recorder struct {
	codes   map[uint64]CodeInfo
	order   []uint64 // code ids, insertion order (for stable output)
	events  []event
	seq     uint64
	wrapped bool
}

// New returns an empty recorder.
func New() *Recorder {
	return &Recorder{codes: map[uint64]CodeInfo{}}
}

// RegisterCode records the identity of a code object the first time it is seen.
func (r *Recorder) RegisterCode(id uint64, file, qualname string, firstLine uint32) {
	if _, ok := r.codes[id]; !ok {
		r.order = append(r.order, id)
	}
	r.codes[id] = CodeInfo{File: file, Qualname: qualname, FirstLine: firstLine}
}

// Record appends one event with the next logical timestamp.
func (r *Recorder) Record(kind uint8, thread uint16, line uint32, codeID uint64) {
	r.events = append(r.events, event{kind: kind, thread: thread, line: line, codeID: codeID, tstamp: r.seq})
	r.seq++
}

// Len reports how many events have been recorded.
func (r *Recorder) Len() int { return len(r.events) }

// Dump writes the accumulated recording to a `.flight` file at path.
func (r *Recorder) Dump(path string, meta Meta) error {
	var out []byte

	// --- header: magic | u16 version | u32 meta len | msgpack(header meta) ---
	headerMeta := mpMap([][2][]byte{
		{mpStr("tool"), mpStr("flight")},
		{mpStr("flight_version"), mpStr(meta.FlightVersion)},
		{mpStr("created_unix_ms"), mpUint(uint64(nowMillis()))},
	})
	out = append(out, magic...)
	out = appendU16(out, formatVersion)
	out = appendU32(out, uint32(len(headerMeta)))
	out = append(out, headerMeta...)

	offset := uint64(len(out))
	type idxEntry struct {
		ty  byte
		off uint64
		ln  uint32
	}
	var index []idxEntry
	writeBlock := func(ty byte, payload []byte) {
		comp := zstdStored(payload)
		index = append(index, idxEntry{ty: ty, off: offset, ln: uint32(len(comp))})
		out = append(out, ty)
		out = appendU32(out, uint32(len(comp)))
		out = append(out, comp...)
		offset += uint64(5 + len(comp))
	}

	// --- META block (named map) ---
	writeBlock(blockMeta, mpMap([][2][]byte{
		{mpStr("python_version"), mpStr(meta.PythonVersion)},
		{mpStr("platform"), mpStr(meta.Platform)},
		{mpStr("argv"), mpStrArray(meta.Argv)},
		{mpStr("cwd"), mpStr(meta.Cwd)},
		{mpStr("flight_version"), mpStr(meta.FlightVersion)},
	}))

	// --- EVENT_RING block (positional: [codes, events, wrapped]) ---
	codePairs := make([][2][]byte, 0, len(r.order))
	for _, id := range r.order {
		ci := r.codes[id]
		codePairs = append(codePairs, [2][]byte{
			mpUint(id),
			mpArray([][]byte{mpStr(ci.File), mpStr(ci.Qualname), mpUint(uint64(ci.FirstLine))}),
		})
	}
	evItems := make([][]byte, 0, len(r.events))
	for _, e := range r.events {
		evItems = append(evItems, mpArray([][]byte{
			mpUint(uint64(e.kind)), mpUint(uint64(e.thread)), mpUint(uint64(e.line)),
			mpUint(e.codeID), mpUint(e.tstamp),
		}))
	}
	ring := mpArray([][]byte{mpMap(codePairs), mpArray(evItems), mpBool(r.wrapped)})
	writeBlock(blockEventRing, ring)

	// --- footer: INDEX block + trailer (u32 index total len | "TLGF") ---
	idxItems := make([][]byte, 0, len(index))
	for _, e := range index {
		idxItems = append(idxItems, mpArray([][]byte{mpUint(uint64(e.ty)), mpUint(e.off), mpUint(uint64(e.ln))}))
	}
	indexStart := offset
	writeBlock(blockIndex, mpArray(idxItems))
	indexTotal := uint32(offset - indexStart)
	out = appendU32(out, indexTotal)
	out = append(out, trailer...)

	return os.WriteFile(path, out, 0o644)
}

// -- little helpers ---------------------------------------------------------

func appendU16(b []byte, v uint16) []byte {
	var t [2]byte
	binary.LittleEndian.PutUint16(t[:], v)
	return append(b, t[:]...)
}

func appendU32(b []byte, v uint32) []byte {
	var t [4]byte
	binary.LittleEndian.PutUint32(t[:], v)
	return append(b, t[:]...)
}
