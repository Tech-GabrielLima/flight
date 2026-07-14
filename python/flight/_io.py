from __future__ import annotations

import builtins
import hashlib
import io
import json
import os
import socket
import subprocess
from typing import Optional

from ._nondet import ReplayDivergence, _reconstruct_exc

_READ_METHODS = ("read", "read1", "readline", "readinto")


def _digest(raw: bytes) -> str:
    return hashlib.blake2b(raw, digest_size=16).hexdigest()


def _as_bytes(data) -> bytes:
    if isinstance(data, str):
        return data.encode("utf-8", "surrogatepass")
    return bytes(data)


class _RecordingFile:

    def __init__(self, raw, recorder, cid: int, hash_above: int):
        object.__setattr__(self, "_raw", raw)
        object.__setattr__(self, "_rec", recorder)
        object.__setattr__(self, "_cid", cid)
        object.__setattr__(self, "_hash_above", hash_above)

    def __getattr__(self, name):
        return getattr(object.__getattribute__(self, "_raw"), name)

    def __setattr__(self, name, value):
        setattr(self._raw, name, value)

    def _source(self, method: str) -> str:
        return f"io.file{self._cid}.{method}"

    def _call(self, method: str, *args, **kwargs):
        raw = object.__getattribute__(self, "_raw")
        outermost = self._rec.enter()
        try:
            result = getattr(raw, method)(*args, **kwargs)
        except BaseException as e:
            self._rec.leave()
            if outermost:
                self._rec.record_raw(self._source(method), "!", type(e).__name__)
            raise
        self._rec.leave()
        if outermost:
            if method == "readinto":
                n = result
                mv = bytes(args[0][:n]) if n else b""
                tag, payload = _encode_read(mv, self._hash_above)
                self._rec.record_raw(self._source("readinto"), tag, payload)
            else:
                tag, payload = _encode_read(result, self._hash_above)
                self._rec.record_raw(self._source(method), tag, payload)
        return result

    def read(self, *a, **k):
        return self._call("read", *a, **k)

    def read1(self, *a, **k):
        return self._call("read1", *a, **k)

    def readline(self, *a, **k):
        return self._call("readline", *a, **k)

    def readinto(self, *a, **k):
        return self._call("readinto", *a, **k)

    def readinto1(self, *a, **k):
        return self._call("readinto", *a, **k)

    def readlines(self, *_a, **_k):
        out = []
        while True:
            line = self.readline()
            if not line:
                break
            out.append(line)
        return out

    def __iter__(self):
        return self

    def __next__(self):
        line = self.readline()
        if not line:
            raise StopIteration
        return line

    def __enter__(self):
        self._raw.__enter__()
        return self

    def __exit__(self, *exc):
        return self._raw.__exit__(*exc)


def _encode_read(result, hash_above: int) -> tuple[str, str]:
    kind = "s" if isinstance(result, str) else "b"
    raw = _as_bytes(result)
    if hash_above and len(raw) > hash_above:
        return ("h", f"{kind}:{len(result)}:{_digest(raw)}")
    if kind == "s":
        return ("s", result)
    return ("b", raw.hex())


class _ReplayFile:

    def __init__(self, tape, cid: int, open_args):
        self._tape = tape
        self._cid = cid
        self._open_args = open_args
        self._live = None
        self.closed = False

    def _source(self, method: str) -> str:
        return f"io.file{self._cid}.{method}"

    def _live_file(self):
        if self._live is None:
            if self._open_args is None:
                raise ReplayDivergence(
                    f"channel {self._cid}: a hashed read needs the original file, "
                    "but the open arguments were not recorded"
                )
            file, mode, kwargs = self._open_args
            try:
                self._live = _ORIG_OPEN(file, mode, **kwargs)
            except OSError as e:
                raise ReplayDivergence(
                    f"channel {self._cid}: a hashed read needs the original source "
                    f"{file!r}, which is unavailable ({e.__class__.__name__}); "
                    "record with io_hash_above=0 to inline it for offline replay"
                ) from e
        return self._live

    def _pull(self, method: str, into=None):
        tag, payload = self._tape.take_raw(self._source(method))
        if tag == "!":
            raise _reconstruct_exc(payload)
        if tag == "h":
            kind, n_s, digest = payload.split(":", 2)
            n = int(n_s)
            data = self._live_file().read(n)
            if len(data) != n or _digest(_as_bytes(data)) != digest:
                raise ReplayDivergence(
                    f"channel {self._cid}: the live source for a hashed read "
                    "no longer matches the recording (length or digest differ)"
                )
        elif tag == "s":
            data = payload
        else:
            data = bytes.fromhex(payload)
        if method == "readinto":
            raw = _as_bytes(data)
            into[: len(raw)] = raw
            return len(raw)
        return data

    def read(self, *_a, **_k):
        return self._pull("read")

    def read1(self, *_a, **_k):
        return self._pull("read1")

    def readline(self, *_a, **_k):
        return self._pull("readline")

    def readinto(self, buf):
        return self._pull("readinto", into=buf)

    def readinto1(self, buf):
        return self._pull("readinto", into=buf)

    def readlines(self, *_a, **_k):
        out = []
        while True:
            line = self.readline()
            if not line:
                break
            out.append(line)
        return out

    def __iter__(self):
        return self

    def __next__(self):
        line = self.readline()
        if not line:
            raise StopIteration
        return line

    def write(self, data):
        return len(data)

    def writelines(self, lines):
        for _ in lines:
            pass

    def seek(self, offset, *_a):
        return offset

    def tell(self):
        return 0

    def flush(self):
        pass

    def truncate(self, *_a):
        return 0

    def close(self):
        self.closed = True
        if self._live is not None:
            self._live.close()

    def fileno(self):
        raise io.UnsupportedOperation("fileno() is not available on a replayed file")

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()
        return False


_ORIG_OPEN = builtins.open


class _Channels:

    def __init__(self):
        self.n = 0

    def next(self) -> int:
        cid = self.n
        self.n += 1
        return cid


class IORecorder:

    def __init__(self, recorder, hash_above: int):
        self._rec = recorder
        self._hash_above = hash_above
        self._chan = _Channels()
        self._saved: list = []

    def _wrap_open(self, orig):
        def flight_open(file, mode="r", *args, **kwargs):
            if not self._rec.is_outermost():
                return orig(file, mode, *args, **kwargs)
            cid = self._chan.next()
            self._rec.record_raw(
                f"io.file{cid}.open",
                "O",
                json.dumps({"file": os.fspath(file) if _fspathable(file) else None}),
            )
            raw = orig(file, mode, *args, **kwargs)
            if _is_readable(mode):
                return _RecordingFile(raw, self._rec, cid, self._hash_above)
            return raw

        return flight_open

    def _wrap_os_read(self, orig):
        def flight_os_read(fd, n):
            outermost = self._rec.enter()
            try:
                data = orig(fd, n)
            except BaseException as e:
                self._rec.leave()
                if outermost:
                    self._rec.record_raw("io.os.read", "!", type(e).__name__)
                raise
            self._rec.leave()
            if outermost:
                tag, payload = _encode_read(data, self._hash_above)
                self._rec.record_raw("io.os.read", tag, payload)
            return data

        return flight_os_read

    def _wrap_subprocess(self, name, orig):
        source = f"io.subprocess.{name}"

        def flight_sub(*args, **kwargs):
            outermost = self._rec.enter()
            try:
                result = orig(*args, **kwargs)
            except BaseException as e:
                self._rec.leave()
                if outermost:
                    self._rec.record_raw(source, "!", type(e).__name__)
                raise
            self._rec.leave()
            if outermost:
                self._rec.record_raw(source, "P", _encode_proc(name, result))
            return result

        return flight_sub

    def _wrap_recv(self, orig):
        def flight_recv(sock, *args, **kwargs):
            outermost = self._rec.enter()
            try:
                data = orig(sock, *args, **kwargs)
            except BaseException as e:
                self._rec.leave()
                if outermost:
                    self._rec.record_raw("io.socket.recv", "!", type(e).__name__)
                raise
            self._rec.leave()
            if outermost:
                tag, payload = _encode_read(data, self._hash_above)
                self._rec.record_raw("io.socket.recv", tag, payload)
            return data

        return flight_recv

    def _wrap_recv_into(self, orig):
        def flight_recv_into(sock, buffer, *args, **kwargs):
            outermost = self._rec.enter()
            try:
                n = orig(sock, buffer, *args, **kwargs)
            except BaseException as e:
                self._rec.leave()
                if outermost:
                    self._rec.record_raw("io.socket.recv_into", "!", type(e).__name__)
                raise
            self._rec.leave()
            if outermost:
                mv = bytes(buffer[:n]) if n else b""
                tag, payload = _encode_read(mv, self._hash_above)
                self._rec.record_raw("io.socket.recv_into", tag, payload)
            return n

        return flight_recv_into

    def install(self) -> None:
        self._patch(builtins, "open", self._wrap_open)
        self._patch(io, "open", self._wrap_open)
        self._patch(os, "read", self._wrap_os_read)
        self._patch(subprocess, "run", lambda o: self._wrap_subprocess("run", o))
        self._patch(
            subprocess, "check_output", lambda o: self._wrap_subprocess("check_output", o)
        )
        self._patch(socket.socket, "recv", self._wrap_recv)
        self._patch(socket.socket, "recv_into", self._wrap_recv_into)

    def _patch(self, obj, attr, make):
        orig = getattr(obj, attr)
        setattr(obj, attr, make(orig))
        self._saved.append((obj, attr, orig))

    def uninstall(self) -> None:
        for obj, attr, orig in reversed(self._saved):
            try:
                setattr(obj, attr, orig)
            except Exception:
                pass
        self._saved.clear()


class IOReplayer:

    def __init__(self, tape):
        self._tape = tape
        self._chan = _Channels()
        self._saved: list = []

    def _wrap_open(self, orig):
        def flight_open(file, mode="r", *args, **kwargs):
            cid = self._chan.next()
            self._tape.take_raw(f"io.file{cid}.open")
            if _is_readable(mode):
                open_args = (file, mode, kwargs) if _fspathable(file) else None
                return _ReplayFile(self._tape, cid, open_args)
            return _ReplayFile(self._tape, cid, None)

        return flight_open

    def _wrap_os_read(self, _orig):
        def flight_os_read(_fd, _n):
            tag, payload = self._tape.take_raw("io.os.read")
            if tag == "!":
                raise _reconstruct_exc(payload)
            if tag == "h":
                raise ReplayDivergence(
                    "io.os.read was recorded in hashed mode and cannot be replayed "
                    "offline; set io_hash_above=0 to inline fd reads"
                )
            return payload if tag == "s" else bytes.fromhex(payload)

        return flight_os_read

    def _wrap_subprocess(self, name, _orig):
        source = f"io.subprocess.{name}"

        def flight_sub(*_args, **_kwargs):
            tag, payload = self._tape.take_raw(source)
            if tag == "!":
                raise _reconstruct_exc(payload)
            return _decode_proc(name, payload)

        return flight_sub

    def _wrap_recv(self, _orig):
        def flight_recv(_sock, *_args, **_kwargs):
            tag, payload = self._tape.take_raw("io.socket.recv")
            if tag == "!":
                raise _reconstruct_exc(payload)
            if tag == "h":
                raise ReplayDivergence(
                    "io.socket.recv was recorded in hashed mode and cannot be "
                    "replayed offline; record with io_hash_above=0"
                )
            return payload if tag == "s" else bytes.fromhex(payload)

        return flight_recv

    def _wrap_recv_into(self, _orig):
        def flight_recv_into(_sock, buffer, *_args, **_kwargs):
            tag, payload = self._tape.take_raw("io.socket.recv_into")
            if tag == "!":
                raise _reconstruct_exc(payload)
            if tag == "h":
                raise ReplayDivergence(
                    "io.socket.recv_into was recorded in hashed mode; record with "
                    "io_hash_above=0 for offline replay"
                )
            data = payload.encode() if tag == "s" else bytes.fromhex(payload)
            buffer[: len(data)] = data
            return len(data)

        return flight_recv_into

    def install(self) -> None:
        self._patch(builtins, "open", self._wrap_open)
        self._patch(io, "open", self._wrap_open)
        self._patch(os, "read", self._wrap_os_read)
        self._patch(subprocess, "run", lambda o: self._wrap_subprocess("run", o))
        self._patch(
            subprocess, "check_output", lambda o: self._wrap_subprocess("check_output", o)
        )
        self._patch(socket.socket, "recv", self._wrap_recv)
        self._patch(socket.socket, "recv_into", self._wrap_recv_into)

    def _patch(self, obj, attr, make):
        orig = getattr(obj, attr)
        setattr(obj, attr, make(orig))
        self._saved.append((obj, attr, orig))

    def uninstall(self) -> None:
        for obj, attr, orig in reversed(self._saved):
            try:
                setattr(obj, attr, orig)
            except Exception:
                pass
        self._saved.clear()


def _enc_stream(s) -> Optional[dict]:
    if s is None:
        return None
    if isinstance(s, str):
        return {"k": "s", "v": s}
    return {"k": "b", "v": bytes(s).hex()}


def _dec_stream(d):
    if d is None:
        return None
    return d["v"] if d["k"] == "s" else bytes.fromhex(d["v"])


def _encode_proc(name: str, result) -> str:
    if name == "check_output":
        return json.dumps({"out": _enc_stream(result)})
    return json.dumps(
        {
            "code": result.returncode,
            "out": _enc_stream(result.stdout),
            "err": _enc_stream(result.stderr),
            "args": result.args if isinstance(result.args, (str, list)) else str(result.args),
        }
    )


def _decode_proc(name: str, payload: str):
    d = json.loads(payload)
    if name == "check_output":
        return _dec_stream(d["out"])
    return subprocess.CompletedProcess(
        args=d["args"],
        returncode=d["code"],
        stdout=_dec_stream(d["out"]),
        stderr=_dec_stream(d["err"]),
    )


def _is_readable(mode) -> bool:
    m = mode if isinstance(mode, str) else "r"
    return ("r" in m) or ("+" in m)


def _fspathable(file) -> bool:
    return isinstance(file, (str, bytes, os.PathLike))
