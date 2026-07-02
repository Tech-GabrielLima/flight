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

import importlib
import json
import os
import platform
import sys
import uuid as _uuid
from typing import Optional

from . import _core


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
        self._cursor = 0

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

    def sources(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for _s, src, _t, _p in self._entries:
            counts[src] = counts.get(src, 0) + 1
        return counts

    def take(self, source: str):
        """Return the next recorded value, checking it was for `source`."""
        if self._cursor >= len(self._entries):
            raise ReplayDivergence(
                f"the recording is exhausted, but the code called {source!r} "
                f"(step {self._cursor}) — control flow diverged from the recorded run"
            )
        seq, src, tag, payload = self._entries[self._cursor]
        if src != source:
            raise ReplayDivergence(
                f"at step {self._cursor}: the recording has {src!r} but the code "
                f"called {source!r} — control flow diverged from the recorded run"
            )
        self._cursor += 1
        return _decode(tag, payload)


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
        # Reentrancy guard: some boundaries call others internally (uuid4 uses
        # os.urandom). We record only the *outermost* interposed call, so each
        # boundary is atomic and replay — which short-circuits the outer call
        # and never makes the inner one — sees the exact same tape.
        self._depth = 0

    def make_wrapper(self, source, orig):
        def wrapper(*args, **kwargs):
            outermost = self._depth == 0
            self._depth += 1
            try:
                result = orig(*args, **kwargs)
            finally:
                self._depth -= 1
            if outermost:
                try:
                    tag, payload = _encode(result)
                    self.entries.append((self._seq, source, tag, payload))
                    self._seq += 1
                except Exception:
                    pass
            return result

        return wrapper


class _Deterministic:
    """Context manager returned by :func:`deterministic`."""

    def __init__(self, path=None):
        self.path = path
        self._recorder = _Recorder()
        self._interposer = _Interposer(self._recorder.make_wrapper)
        self.path_written: Optional[str] = None

    def __enter__(self) -> "_Deterministic":
        self._interposer.install()
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self._interposer.uninstall()
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


def deterministic(path=None) -> _Deterministic:
    """Record the non-determinism of the enclosed block into a `.flight`.

        with flight.deterministic("run.flight"):
            result = do_work()          # uses time / random / uuid …

    Replay it later — even in another process — and it re-runs bit-for-bit::

        replayed = flight.replay("run.flight", do_work)
        assert replayed == result
    """
    return _Deterministic(path)


def replay(flight_path, fn, *args, **kwargs):
    """Re-run `fn` feeding it the non-determinism recorded in `flight_path`.

    Returns `fn`'s result; raises :class:`ReplayDivergence` if the code's calls
    diverge from the recording."""
    from ._read import read

    return replay_tape(read(flight_path).tape(), fn, *args, **kwargs)


def replay_tape(tape: Tape, fn, *args, **kwargs):
    """Like :func:`replay`, but from an in-memory :class:`Tape`."""
    interposer = _Interposer(lambda source, orig: _replay_wrapper(tape, source))
    interposer.install()
    try:
        return fn(*args, **kwargs)
    finally:
        interposer.uninstall()


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
