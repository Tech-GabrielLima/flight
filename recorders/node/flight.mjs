// A Node.js recorder writing the SAME `.flight` black-box format the
// Python/Rust engine reads — with zero dependencies.
//
// Like the Go recorder, this leans on two tricks so the standard library is
// enough: a tiny msgpack encoder for exactly the types the format uses, and a
// "stored" zstd frame (valid zstd, raw/uncompressed blocks) so no compressor is
// needed. The Rust reader decodes the result like any other `.flight`.
//
// Usage:
//   import { Recorder, KindStart } from "./flight.mjs";
//   const rec = new Recorder();
//   rec.registerCode(1, "app.js", "main", 1);
//   rec.record(KindStart, 0, 0, 1);
//   await rec.dump("out.flight");

import { writeFileSync } from "node:fs";
import process from "node:process";

export const KindStart = 1;
export const KindReturn = 2;
export const KindLine = 3;
export const KindRaise = 4;
export const KindReraise = 5;
export const KindUnwind = 6;

const MAGIC = Buffer.from("FLGT");
const TRAILER = Buffer.from("TLGF");
const FORMAT_VERSION = 1;
const BLOCK_META = 0x01;
const BLOCK_EVENT_RING = 0x06;
const BLOCK_INDEX = 0x70;

// -- tiny msgpack encoder ---------------------------------------------------

function mpUint(n) {
  n = Number(n);
  if (n < 0x80) return Buffer.from([n]);
  if (n <= 0xff) return Buffer.from([0xcc, n]);
  if (n <= 0xffff) {
    const b = Buffer.alloc(3);
    b[0] = 0xcd;
    b.writeUInt16BE(n, 1);
    return b;
  }
  if (n <= 0xffffffff) {
    const b = Buffer.alloc(5);
    b[0] = 0xce;
    b.writeUInt32BE(n, 1);
    return b;
  }
  const b = Buffer.alloc(9);
  b[0] = 0xcf;
  b.writeBigUInt64BE(BigInt(n), 1);
  return b;
}

function mpStr(s) {
  const body = Buffer.from(String(s), "utf-8");
  const n = body.length;
  if (n < 32) return Buffer.concat([Buffer.from([0xa0 | n]), body]);
  if (n < 256) return Buffer.concat([Buffer.from([0xd9, n]), body]);
  if (n < 65536) {
    const h = Buffer.alloc(3);
    h[0] = 0xda;
    h.writeUInt16BE(n, 1);
    return Buffer.concat([h, body]);
  }
  const h = Buffer.alloc(5);
  h[0] = 0xdb;
  h.writeUInt32BE(n, 1);
  return Buffer.concat([h, body]);
}

function mpBool(v) {
  return Buffer.from([v ? 0xc3 : 0xc2]);
}

function mpArrayHeader(n) {
  if (n < 16) return Buffer.from([0x90 | n]);
  if (n < 65536) {
    const h = Buffer.alloc(3);
    h[0] = 0xdc;
    h.writeUInt16BE(n, 1);
    return h;
  }
  const h = Buffer.alloc(5);
  h[0] = 0xdd;
  h.writeUInt32BE(n, 1);
  return h;
}

function mpArray(items) {
  return Buffer.concat([mpArrayHeader(items.length), ...items]);
}

function mpMapHeader(n) {
  if (n < 16) return Buffer.from([0x80 | n]);
  if (n < 65536) {
    const h = Buffer.alloc(3);
    h[0] = 0xde;
    h.writeUInt16BE(n, 1);
    return h;
  }
  const h = Buffer.alloc(5);
  h[0] = 0xdf;
  h.writeUInt32BE(n, 1);
  return h;
}

function mpMap(pairs) {
  const parts = [mpMapHeader(pairs.length)];
  for (const [k, v] of pairs) parts.push(k, v);
  return Buffer.concat(parts);
}

// -- stored zstd frame (raw blocks) -----------------------------------------

function blockHeader(size, last) {
  let v = (size << 3) >>> 0;
  if (last) v |= 1;
  const b = Buffer.alloc(4);
  b.writeUInt32LE(v >>> 0, 0);
  return b.subarray(0, 3);
}

function zstdStored(data) {
  const head = Buffer.alloc(9);
  head[0] = 0x28;
  head[1] = 0xb5;
  head[2] = 0x2f;
  head[3] = 0xfd; // magic
  head[4] = 0xa0; // FCS_flag=2 (4 bytes), Single_Segment=1
  head.writeUInt32LE(data.length, 5);
  const parts = [head];
  const CHUNK = 65536;
  if (data.length === 0) {
    parts.push(blockHeader(0, true));
    return Buffer.concat(parts);
  }
  for (let i = 0; i < data.length; i += CHUNK) {
    const end = Math.min(i + CHUNK, data.length);
    parts.push(blockHeader(end - i, end >= data.length));
    parts.push(data.subarray(i, end));
  }
  return Buffer.concat(parts);
}

// -- recorder ---------------------------------------------------------------

export class Recorder {
  constructor() {
    this.codes = new Map(); // id -> {file, qualname, firstLine}
    this.order = [];
    this.events = [];
    this.seq = 0;
    this.wrapped = false;
  }

  registerCode(id, file, qualname, firstLine) {
    if (!this.codes.has(id)) this.order.push(id);
    this.codes.set(id, { file, qualname, firstLine });
  }

  record(kind, thread, line, codeId) {
    this.events.push({ kind, thread, line, codeId, tstamp: this.seq });
    this.seq += 1;
  }

  get length() {
    return this.events.length;
  }

  dump(path, meta = defaultMeta()) {
    const headerMeta = mpMap([
      [mpStr("tool"), mpStr("flight")],
      [mpStr("flight_version"), mpStr(meta.flightVersion)],
      [mpStr("created_unix_ms"), mpUint(Date.now())],
    ]);
    const parts = [MAGIC];
    const ver = Buffer.alloc(2);
    ver.writeUInt16LE(FORMAT_VERSION, 0);
    parts.push(ver);
    const mlen = Buffer.alloc(4);
    mlen.writeUInt32LE(headerMeta.length, 0);
    parts.push(mlen, headerMeta);

    let offset = MAGIC.length + 2 + 4 + headerMeta.length;
    const index = [];
    const writeBlock = (ty, payload) => {
      const comp = zstdStored(payload);
      index.push({ ty, off: offset, len: comp.length });
      const bh = Buffer.alloc(5);
      bh[0] = ty;
      bh.writeUInt32LE(comp.length, 1);
      parts.push(bh, comp);
      offset += 5 + comp.length;
    };

    // META block
    writeBlock(
      BLOCK_META,
      mpMap([
        [mpStr("python_version"), mpStr(meta.pythonVersion)],
        [mpStr("platform"), mpStr(meta.platform)],
        [mpStr("argv"), mpArray(meta.argv.map(mpStr))],
        [mpStr("cwd"), mpStr(meta.cwd)],
        [mpStr("flight_version"), mpStr(meta.flightVersion)],
      ])
    );

    // EVENT_RING block: [codes, events, wrapped]
    const codePairs = this.order.map((id) => {
      const ci = this.codes.get(id);
      return [mpUint(id), mpArray([mpStr(ci.file), mpStr(ci.qualname), mpUint(ci.firstLine)])];
    });
    const evItems = this.events.map((e) =>
      mpArray([mpUint(e.kind), mpUint(e.thread), mpUint(e.line), mpUint(e.codeId), mpUint(e.tstamp)])
    );
    writeBlock(BLOCK_EVENT_RING, mpArray([mpMap(codePairs), mpArray(evItems), mpBool(this.wrapped)]));

    // footer: INDEX block + trailer
    const idxItems = index.map((e) => mpArray([mpUint(e.ty), mpUint(e.off), mpUint(e.len)]));
    const indexStart = offset;
    writeBlock(BLOCK_INDEX, mpArray(idxItems));
    const indexTotal = offset - indexStart;
    const tl = Buffer.alloc(4);
    tl.writeUInt32LE(indexTotal, 0);
    parts.push(tl, TRAILER);

    writeFileSync(path, Buffer.concat(parts));
  }
}

export function defaultMeta() {
  return {
    pythonVersion: `node-${process.version}`,
    platform: `${process.platform}/${process.arch}`,
    argv: process.argv,
    cwd: process.cwd(),
    flightVersion: "0.0.1-node",
  };
}
