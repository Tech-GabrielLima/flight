"""Phase 3, rung 2 — deterministic replay (TECHNICAL.md §4.1–4.2).

A program is a deterministic function of its non-deterministic inputs. Record
*only* those — the clock, randomness, uuids, `os.urandom`/`secrets`, a few `os`
calls — at the edges (cheap), and the whole run replays bit-for-bit by feeding
the recorded values back in order. That's the `rr` model, at the level of Python
APIs rather than syscalls.

**How.** `with flight.deterministic(path):` patches an allowlist of boundary
functions; during the run each interposed call records its result to a NONDET
tape. `flight.replay(path, fn)` patches the same boundaries to *return* the
recorded values in order; if the code asks for a boundary in a different order
than recorded — i.e. control flow diverged — it raises [`ReplayDivergence`]
pointing at the exact step, which is itself a strong debugging signal.

Interposition is by module attribute (`time.time`, `random.random`, …), so it
covers `module.func()` and internal instance calls but not names imported with
`from module import func` *before* the scope. Files, sockets and subprocess are
staged (their state is larger); the clock/randomness/uuid class of
non-determinism — flaky tests, time bombs, "fails 1% of the time" — is covered.
"""

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
    """The recording *thread channel* of the calling thread (0 for the main
    thread and any thread not started under a recording). Threads started inside
    a `deterministic()`/`replay()` scope are numbered in start order by
    :mod:`_threads`, which stamps the id on the Thread object; each thread then
    records/replays its boundary calls on its own cursor, so concurrent,
    unsynchronized calls (two threads reading the clock) never fight over one
    global order. Cross-thread ordering that *does* matter — lock acquisition —
    is handled separately, by replaying the recorded acquisition schedule."""
    return getattr(threading.current_thread(), "_flight_channel", 0)


class ReplayDivergence(RuntimeError):
    """Raised on replay when the code's calls diverge from the recording."""


# Boundary functions interposed, as (module, attribute). Each is nullary or has
# simple args and returns a simple, encodable value.
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


# -- value codec (Python owns encoding; the format just stores strings) -----


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
    return ("r", repr(value))  # best-effort fallback


def _reconstruct_exc(name: str) -> BaseException:
    """Rebuild a recorded exception by type name (builtin types are matched
    exactly; anything else replays as a RuntimeError carrying the name)."""
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


# -- the tape ---------------------------------------------------------------


class Tape:
    """The recorded non-determinism, for replay. `entries` are
    `(seq, source, tag, payload)` in call order."""

    def __init__(self, entries):
        self._entries = list(entries)
        # Per-thread cursors, built lazily so `pop_control` can strip the
        # scope-level control entries (asyncio/threads order) first. Each thread
        # channel replays its own boundary calls in its own recorded order.
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
        """The recorded entries as ``(seq, source, tag, payload)`` tuples, in
        order — for tools that compare or minimize tapes (`_diff`, `_ddmin`)."""
        return list(self._entries)

    def sources(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for _s, src, _t, _p in self._entries:
            _ch, real = _split_channel(src)
            counts[real] = counts.get(real, 0) + 1
        return counts

    def pop_control(self, source: str) -> Optional[str]:
        """Remove and return the payload of a scope-level *control* entry (the
        asyncio / thread ordering, written once when the scope closed). Called
        before replay begins, so those entries never sit in a per-thread cursor.
        Returns None if absent (the scope used no asyncio / threads)."""
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
        """Advance past this thread's next entry, checking it was for `source`;
        return its raw ``(tag, payload)``. Raises :class:`ReplayDivergence` on a
        mismatch — the exact step where this thread left the recorded run."""
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
        """Return the next recorded value, checking it was for `source`."""
        tag, payload = self._next(source)
        if tag == "!":
            # The recorded call raised; re-raise the same exception type so the
            # code's control flow (its except clauses) replays faithfully.
            raise _reconstruct_exc(payload)
        return _decode(tag, payload)

    def take_raw(self, source: str) -> tuple[str, str]:
        """Like :meth:`take` but return the raw ``(tag, payload)`` — for I/O
        channels (`_io`) that own their own codec (bytes, text, hashed reads)
        and reconstruct richer objects than the scalar value codec."""
        return self._next(source)


def _split_channel(src: str) -> tuple[int, str]:
    """Parse a recorded source into ``(thread_channel, real_source)``. The main
    thread (channel 0) records bare sources for backward compatibility; other
    threads prefix ``@<channel>#``."""
    if src.startswith("@"):
        marker = src.find("#")
        if marker != -1:
            try:
                return int(src[1:marker]), src[marker + 1 :]
            except ValueError:
                pass
    return 0, src


# -- interposition ----------------------------------------------------------


class _Interposer:
    """Installs/removes wrappers over the boundary functions."""

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
        # The tape append is shared across threads, so it is lock-guarded; the
        # seq counter is the global order. The reentrancy guard, by contrast, is
        # *per thread* (thread-local depth): some boundaries call others
        # internally (uuid4 uses os.urandom; subprocess.run reads pipes via
        # os.read), so we record only the *outermost* interposed call per thread
        # — each boundary is atomic and replay, which short-circuits the outer
        # call and never makes the inner one, sees the same per-thread lane. The
        # guard is shared across the scalar boundaries here and the I/O channels
        # in `_io`.
        self._append_lock = threading.Lock()
        self._tls = threading.local()

    def _record(self, source, tag, payload):
        ch = _current_channel()
        if ch:
            source = f"@{ch}#{source}"
        with self._append_lock:
            self.entries.append((self._seq, source, tag, payload))
            self._seq += 1

    # -- shared guarded recording (used by scalar boundaries and by `_io`) ---

    def _depth(self) -> int:
        return getattr(self._tls, "depth", 0)

    def is_outermost(self) -> bool:
        return self._depth() == 0

    def enter(self) -> bool:
        """Enter an interposed call; returns True if it is the outermost one
        *on this thread* (the reentrancy guard is per thread)."""
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
        """Record a pre-encoded entry (I/O channels own their own codec)."""
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
                # A boundary that *raises* is part of the behaviour to replay
                # (e.g. random.randint(5, 1) -> ValueError caught by the code).
                if outermost:
                    self.record_exc(source, e)
                raise
            self.leave()
            if outermost:
                self.record_value(source, result)
            return result

        return wrapper


#: Default: reads larger than this many bytes are stored as length+digest
#: ("record what was read, hash the rest") instead of inline content.
DEFAULT_IO_HASH_ABOVE = 256 * 1024


class _Deterministic:
    """Context manager returned by :func:`deterministic`."""

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
        # Thread channels first (so any boundary recorded on a worker thread is
        # tagged with the right lane), then the boundaries themselves.
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
            self._threads.finalize()  # append the lock-acquisition order entry
        if self._aio is not None:
            self._aio.finalize()  # append the asyncio completion-order entry
        # If the block crashed, capture the crash black box *and* the tape into
        # one file, so `flight repro` can rebuild the args AND replay the exact
        # non-determinism — reproducing a time/random-dependent crash.
        if exc is not None:
            self._write_crash(exc, tb)
        else:
            self._write()
        return False  # never suppress the user's exception

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
            # Never let recording break the dying program (P1); fall back to
            # writing just the tape.
            self._write()


def deterministic(path=None, *, record_io: bool = True, io_hash_above=None) -> _Deterministic:
    """Record the non-determinism of the enclosed block into a `.flight`.

        with flight.deterministic("run.flight"):
            result = do_work()          # uses time / random / uuid / files …

    Replay it later — even in another process — and it re-runs bit-for-bit::

        replayed = flight.replay("run.flight", do_work)
        assert replayed == result

    Beyond the scalar boundaries (clock/random/uuid), `record_io=True` (default)
    also records **what the code read** — files, ``os.read`` pipes and
    subprocess output — plus the asyncio task-completion order, so an I/O- or
    schedule-dependent run replays too. Reads larger than ``io_hash_above`` bytes
    are stored as a length + digest instead of their content (verified against
    the live source on replay); pass ``io_hash_above=0`` to inline everything for
    fully offline replay.
    """
    return _Deterministic(path, record_io=record_io, io_hash_above=io_hash_above)


def replay(flight_path, fn, *args, **kwargs):
    """Re-run `fn` feeding it the non-determinism recorded in `flight_path`.

    Returns `fn`'s result; raises :class:`ReplayDivergence` if the code's calls
    diverge from the recording."""
    from ._read import read

    return replay_tape(read(flight_path).tape(), fn, *args, **kwargs)


def replay_tape(tape: Tape, fn, *args, **kwargs):
    """Like :func:`replay`, but from an in-memory :class:`Tape`.

    Interposes the same scalar boundaries *and* the I/O channels (files,
    ``os.read``, subprocess) and asyncio scheduling, so a run that read files or
    spawned processes replays without touching the real world — reads come from
    the tape, writes are swallowed. Raises :class:`ReplayDivergence` at the exact
    step the code's calls leave the recorded order."""
    from ._io import IOReplayer
    from ._asyncio import AsyncioReplayer
    from ._threads import ThreadReplayer

    # Pull the scope-level control entries off the tape before it is partitioned
    # into per-thread lanes.
    order_payload = tape.pop_control("threads.order")
    threads_order = json.loads(order_payload) if order_payload else None

    threads = ThreadReplayer(threads_order)
    interposer = _Interposer(lambda source, orig: _replay_wrapper(tape, source))
    io = IOReplayer(tape)
    aio = AsyncioReplayer(tape)  # pops asyncio.order in __init__
    threads.install()  # number threads + gate lock acquisitions before fn runs
    interposer.install()
    io.install()
    aio.install()
    try:
        result = fn(*args, **kwargs)
        aio.finalize()  # verify the asyncio completion order matched
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
