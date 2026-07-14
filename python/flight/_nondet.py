from __future__ import annotations

import builtins
import importlib
import json
import os
import platform
import sys
import threading
import uuid as _uuid
from typing import Optional

from . import _core


def _current_channel() -> int:
    return getattr(threading.current_thread(), "_flight_channel", 0)


class ReplayDivergence(RuntimeError):
    pass


_SOURCES: tuple[tuple[str, str], ...] = (
    ("time", "time"),
    ("time", "monotonic"),
    ("time", "perf_counter"),
    ("time", "time_ns"),
    ("time", "monotonic_ns"),
    ("time", "perf_counter_ns"),
    ("random", "random"),
    ("random", "randint"),
    ("random", "uniform"),
    ("random", "randrange"),
    ("random", "getrandbits"),
    ("os", "urandom"),
    ("os", "getpid"),
    ("os", "getenv"),
    ("uuid", "uuid4"),
    ("secrets", "token_bytes"),
    ("secrets", "token_hex"),
    ("secrets", "token_urlsafe"),
    ("secrets", "randbelow"),
)


def _encode(value) -> tuple[str, str]:
    if isinstance(value, bool):
        return ("o", "1" if value else "0")
    if isinstance(value, int):
        return ("i", str(value))
    if isinstance(value, float):
        return ("f", repr(value))
    if isinstance(value, str):
        return ("s", value)
    if isinstance(value, (bytes, bytearray)):
        return ("b", bytes(value).hex())
    if value is None:
        return ("n", "")
    if isinstance(value, _uuid.UUID):
        return ("u", str(value))
    if isinstance(value, dict):
        return ("d", json.dumps(value))
    return ("r", repr(value))


def _reconstruct_exc(name: str) -> BaseException:
    exc = getattr(builtins, name, None)
    if isinstance(exc, type) and issubclass(exc, BaseException):
        return exc("replayed from flight recording")
    return RuntimeError(f"replayed {name} from flight recording")


def _decode(tag: str, payload: str):
    if tag == "o":
        return payload == "1"
    if tag == "i":
        return int(payload)
    if tag == "f":
        return float(payload)
    if tag == "s":
        return payload
    if tag == "b":
        return bytes.fromhex(payload)
    if tag == "n":
        return None
    if tag == "u":
        return _uuid.UUID(payload)
    if tag == "d":
        return json.loads(payload)
    return payload


class Tape:

    def __init__(self, entries):
        self._entries = list(entries)
        self._by_channel: Optional[dict[int, list[tuple[str, str]]]] = None
        self._pos: dict[int, int] = {}
        self._lock = threading.Lock()

    def __len__(self) -> int:
        return len(self._entries)

    @classmethod
    def from_json(cls, text: str) -> "Tape":
        data = json.loads(text)
        return cls((e["seq"], e["source"], e["tag"], e["payload"]) for e in data)

    def to_json(self) -> str:
        return json.dumps(
            [{"seq": s, "source": src, "tag": t, "payload": p} for s, src, t, p in self._entries]
        )

    def rows(self) -> list[tuple]:
        return list(self._entries)

    def sources(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for _s, src, _t, _p in self._entries:
            _ch, real = _split_channel(src)
            counts[real] = counts.get(real, 0) + 1
        return counts

    def pop_control(self, source: str) -> Optional[str]:
        for i, (_seq, src, _tag, payload) in enumerate(self._entries):
            if src == source:
                del self._entries[i]
                return payload
        return None

    def _partition(self) -> dict[int, list[tuple[str, str]]]:
        if self._by_channel is None:
            table: dict[int, list[tuple[str, str]]] = {}
            for _seq, src, tag, payload in self._entries:
                ch, real = _split_channel(src)
                table.setdefault(ch, []).append((real, tag, payload))
            self._by_channel = table
        return self._by_channel

    def _next(self, source: str) -> tuple[str, str]:
        ch = _current_channel()
        with self._lock:
            lane = self._partition().get(ch, ())
            pos = self._pos.get(ch, 0)
            if pos >= len(lane):
                raise ReplayDivergence(
                    f"thread channel {ch}: the recording is exhausted, but the code "
                    f"called {source!r} — control flow diverged from the recorded run"
                )
            real, tag, payload = lane[pos]
            if real != source:
                raise ReplayDivergence(
                    f"thread channel {ch}, step {pos}: the recording has {real!r} "
                    f"but the code called {source!r} — control flow diverged"
                )
            self._pos[ch] = pos + 1
        return tag, payload

    def take(self, source: str):
        tag, payload = self._next(source)
        if tag == "!":
            raise _reconstruct_exc(payload)
        return _decode(tag, payload)

    def take_raw(self, source: str) -> tuple[str, str]:
        return self._next(source)


def _split_channel(src: str) -> tuple[int, str]:
    if src.startswith("@"):
        marker = src.find("#")
        if marker != -1:
            try:
                return int(src[1:marker]), src[marker + 1 :]
            except ValueError:
                pass
    return 0, src


class _Interposer:

    def __init__(self, make_wrapper):
        self._make = make_wrapper
        self._saved: list[tuple[object, str, object]] = []

    def install(self) -> None:
        for mod_name, attr in _SOURCES:
            try:
                mod = importlib.import_module(mod_name)
                orig = getattr(mod, attr)
            except (ImportError, AttributeError):
                continue
            source = f"{mod_name}.{attr}"
            setattr(mod, attr, self._make(source, orig))
            self._saved.append((mod, attr, orig))

    def uninstall(self) -> None:
        for mod, attr, orig in reversed(self._saved):
            try:
                setattr(mod, attr, orig)
            except Exception:
                pass
        self._saved.clear()


class _Recorder:
    def __init__(self):
        self.entries: list[tuple[int, str, str, str]] = []
        self._seq = 0
        self._append_lock = threading.Lock()
        self._tls = threading.local()

    def _record(self, source, tag, payload):
        ch = _current_channel()
        if ch:
            source = f"@{ch}#{source}"
        with self._append_lock:
            self.entries.append((self._seq, source, tag, payload))
            self._seq += 1


    def _depth(self) -> int:
        return getattr(self._tls, "depth", 0)

    def is_outermost(self) -> bool:
        return self._depth() == 0

    def enter(self) -> bool:
        depth = self._depth()
        self._tls.depth = depth + 1
        return depth == 0

    def leave(self) -> None:
        self._tls.depth = self._depth() - 1

    def record_value(self, source, result) -> None:
        try:
            tag, payload = _encode(result)
        except Exception:
            tag, payload = "r", repr(result)
        self._record(source, tag, payload)

    def record_raw(self, source, tag, payload) -> None:
        self._record(source, tag, payload)

    def record_exc(self, source, exc) -> None:
        self._record(source, "!", type(exc).__name__)

    def make_wrapper(self, source, orig):
        def wrapper(*args, **kwargs):
            outermost = self.enter()
            try:
                result = orig(*args, **kwargs)
            except BaseException as e:
                self.leave()
                if outermost:
                    self.record_exc(source, e)
                raise
            self.leave()
            if outermost:
                self.record_value(source, result)
            return result

        return wrapper


DEFAULT_IO_HASH_ABOVE = 256 * 1024


class _Deterministic:

    def __init__(self, path=None, *, record_io: bool = True, io_hash_above: Optional[int] = None):
        self.path = path
        self._recorder = _Recorder()
        self._interposer = _Interposer(self._recorder.make_wrapper)
        self.path_written: Optional[str] = None
        self._record_io = record_io
        hash_above = DEFAULT_IO_HASH_ABOVE if io_hash_above is None else io_hash_above
        self._io = None
        self._aio = None
        self._threads = None
        if record_io:
            from ._io import IORecorder
            from ._asyncio import AsyncioRecorder
            from ._threads import ThreadRecorder

            self._io = IORecorder(self._recorder, hash_above)
            self._aio = AsyncioRecorder(self._recorder)
            self._threads = ThreadRecorder(self._recorder)

    def __enter__(self) -> "_Deterministic":
        if self._threads is not None:
            self._threads.install()
        self._interposer.install()
        if self._io is not None:
            self._io.install()
            self._aio.install()
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if self._io is not None:
            self._io.uninstall()
            self._aio.uninstall()
        self._interposer.uninstall()
        if self._threads is not None:
            self._threads.uninstall()
            self._threads.finalize()
        if self._aio is not None:
            self._aio.finalize()
        if exc is not None:
            self._write_crash(exc, tb)
        else:
            self._write()
        return False

    @property
    def tape(self) -> Tape:
        return Tape(self._recorder.entries)

    def _write(self) -> None:
        from . import __version__

        path = self.path or _default_path()
        try:
            _core.dump_nondet(
                str(path),
                platform.python_version(),
                platform.platform(),
                list(sys.argv),
                _cwd(),
                __version__,
                self._recorder.entries,
                [],
            )
            self.path_written = str(path)
            print(f"[flight] recorded deterministic run {path}", file=sys.stderr)
        except Exception:
            pass

    def _write_crash(self, exc, tb) -> None:
        path = self.path or _default_path()
        try:
            from ._capture import build_payload
            from ._config import Config
            from . import __version__

            sources, excs, frames, objects = build_payload(exc, tb, Config())
            _core.dump_crash(
                str(path),
                platform.python_version(),
                platform.platform(),
                list(sys.argv),
                _cwd(),
                __version__,
                sources,
                excs,
                frames,
                objects,
                self._recorder.entries,
            )
            self.path_written = str(path)
            print(f"[flight] recorded deterministic crash {path}", file=sys.stderr)
        except Exception:
            self._write()


def deterministic(path=None, *, record_io: bool = True, io_hash_above=None) -> _Deterministic:
    return _Deterministic(path, record_io=record_io, io_hash_above=io_hash_above)


def replay(flight_path, fn, *args, **kwargs):
    from ._read import read

    return replay_tape(read(flight_path).tape(), fn, *args, **kwargs)


def replay_tape(tape: Tape, fn, *args, **kwargs):
    from ._io import IOReplayer
    from ._asyncio import AsyncioReplayer
    from ._threads import ThreadReplayer

    order_payload = tape.pop_control("threads.order")
    threads_order = json.loads(order_payload) if order_payload else None

    threads = ThreadReplayer(threads_order)
    interposer = _Interposer(lambda source, orig: _replay_wrapper(tape, source))
    io = IOReplayer(tape)
    aio = AsyncioReplayer(tape)
    threads.install()
    interposer.install()
    io.install()
    aio.install()
    try:
        result = fn(*args, **kwargs)
        aio.finalize()
        return result
    finally:
        io.uninstall()
        aio.uninstall()
        interposer.uninstall()
        threads.uninstall()


def _replay_wrapper(tape: Tape, source: str):
    def wrapper(*_args, **_kwargs):
        return tape.take(source)

    return wrapper


def _default_path() -> str:
    import time as _time

    return f"flight-run-{os.getpid()}-{int(_time.time() * 1000)}.flight"


def _cwd() -> str:
    try:
        return os.getcwd()
    except Exception:
        return ""
