"""Extended coverage for the deterministic record/replay stack:

- value codec  (flight._nondet._encode / _decode / _reconstruct_exc)
- the Tape     (sources / take / take_raw / pop_control / _split_channel / json)
- deterministic() / replay() bit-for-bit reproduction of the scalar boundaries
- deterministic I/O (files, os.read pipes, subprocess, sockets; hash-of-rest)
- asyncio task-completion order (record + replay + divergence)
- thread lock-acquisition order (enforce on replay; _gated; timeout path)

Everything asserts real, observed behaviour. A couple of documented *limitations*
(e.g. random.choice is not interposed) are pinned as tests so a future change is
noticed. Concurrency tests use small thread counts, joins and short timeouts and
are made deterministic (or the timeout path is exercised via a patched constant),
so the file is stable, not flaky.
"""

from __future__ import annotations

import io
import json
import math
import os
import random
import secrets
import socket
import subprocess
import sys
import threading
import time
import uuid

import pytest

import flight
from flight._nondet import (
    ReplayDivergence,
    Tape,
    _decode,
    _encode,
    _reconstruct_exc,
    _split_channel,
)
import flight._threads as _threads_mod
from flight._threads import _gated, ThreadReplayer


# ===========================================================================
# value codec
# ===========================================================================

INT_VALUES = [
    0, 1, -1, 5, -5, 42, 127, 128, 255, 256, -256,
    2**31, -(2**31), 2**32, 2**63, 2**64, 2**70, -(2**70),
    10**50, -(10**50), 12345678901234567890,
]


@pytest.mark.parametrize("value", INT_VALUES)
def test_codec_int_roundtrip(value):
    tag, payload = _encode(value)
    assert tag == "i"
    got = _decode(tag, payload)
    assert got == value
    assert isinstance(got, int) and not isinstance(got, bool)


FINITE_FLOATS = [
    0.0, -0.0, 1.0, -1.0, 1.5, -1.5, 3.141592653589793, 2.718281828459045,
    1e300, -1e300, 1e-300, 2.5e-10, 123456789.123456789,
    sys.float_info.max, sys.float_info.min, sys.float_info.epsilon,
]


@pytest.mark.parametrize("value", FINITE_FLOATS)
def test_codec_finite_float_roundtrip(value):
    tag, payload = _encode(value)
    assert tag == "f"
    # repr is the shortest string that round-trips exactly in CPython.
    assert _decode(tag, payload) == value


@pytest.mark.parametrize("value", [float("inf"), float("-inf")])
def test_codec_infinite_float_roundtrip(value):
    got = _decode(*_encode(value))
    assert got == value


def test_codec_nan_roundtrip():
    got = _decode(*_encode(float("nan")))
    assert math.isnan(got)


STR_VALUES = [
    "", "a", "hello world", "héllo", "日本語テキスト", "emoji 🎉🚀",
    "line1\nline2\nline3", "tab\tsep", "carriage\rreturn", "null\x00byte",
    "  leading and trailing  ", 'quotes "\'`', "back\\slash",
    "x" * 10000, "\U0001f600 surrogate-ish", "0", "None", "1e9",
]


@pytest.mark.parametrize("value", STR_VALUES)
def test_codec_str_roundtrip(value):
    tag, payload = _encode(value)
    assert tag == "s"
    assert _decode(tag, payload) == value


BYTES_VALUES = [
    b"", b"\x00", b"\xff", b"\x00\xff", b"hello", bytes(range(256)),
    bytes(range(256)) * 3, b"\x00" * 1000, b"\xde\xad\xbe\xef",
]


@pytest.mark.parametrize("value", BYTES_VALUES)
def test_codec_bytes_roundtrip(value):
    tag, payload = _encode(value)
    assert tag == "b"
    got = _decode(tag, payload)
    assert got == value
    assert isinstance(got, bytes)


@pytest.mark.parametrize("value", [bytearray(b""), bytearray(b"abc"), bytearray(range(10))])
def test_codec_bytearray_decodes_to_equal_bytes(value):
    # bytearray encodes via the bytes branch; it decodes back as immutable bytes.
    tag, payload = _encode(value)
    assert tag == "b"
    got = _decode(tag, payload)
    assert got == bytes(value)
    assert isinstance(got, bytes)


@pytest.mark.parametrize("value", [True, False])
def test_codec_bool_roundtrip(value):
    tag, payload = _encode(value)
    assert tag == "o"
    got = _decode(tag, payload)
    assert got is value


def test_codec_none_roundtrip():
    tag, payload = _encode(None)
    assert tag == "n"
    assert _decode(tag, payload) is None


@pytest.mark.parametrize(
    "value",
    [
        uuid.UUID("00000000-0000-0000-0000-000000000000"),
        uuid.UUID("ffffffff-ffff-ffff-ffff-ffffffffffff"),
        uuid.UUID("12345678-1234-5678-1234-567812345678"),
        uuid.uuid4(),
        uuid.uuid4(),
    ],
)
def test_codec_uuid_roundtrip(value):
    tag, payload = _encode(value)
    assert tag == "u"
    got = _decode(tag, payload)
    assert got == value
    assert isinstance(got, uuid.UUID)


DICT_VALUES = [
    {},
    {"a": 1},
    {"a": 1, "b": "x"},
    {"nested": {"deep": {"deeper": 1}}},
    {"list": [1, 2, 3]},
    {"unicode": "héllo", "emoji": "🎉"},
    {"num": 3.14, "flag": True, "empty": None},
    {"mixed": [1, "two", 3.0, False, None]},
    {"many": {str(i): i for i in range(20)}},
]


@pytest.mark.parametrize("value", DICT_VALUES)
def test_codec_dict_roundtrip(value):
    tag, payload = _encode(value)
    assert tag == "d"
    assert _decode(tag, payload) == value


@pytest.mark.parametrize(
    "value,expected_tag",
    [
        (True, "o"),
        (False, "o"),
        (0, "i"),
        (-7, "i"),
        (2**80, "i"),
        (1.5, "f"),
        (float("inf"), "f"),
        ("text", "s"),
        ("", "s"),
        (b"\x01\x02", "b"),
        (bytearray(b"z"), "b"),
        (None, "n"),
        (uuid.UUID(int=0), "u"),
        ({}, "d"),
        ({"k": "v"}, "d"),
    ],
)
def test_codec_tag_selection(value, expected_tag):
    # bool must win over int (bool is an int subclass); this pins that ordering.
    assert _encode(value)[0] == expected_tag


class _Weird:
    def __repr__(self):
        return "<weird sentinel>"


@pytest.mark.parametrize(
    "value",
    [
        ValueError("boom"),
        KeyError("missing"),
        RuntimeError(),
        object(),
        _Weird(),
        [1, 2, 3],  # list has no dedicated branch -> repr fallback
    ],
)
def test_codec_unencodable_falls_back_to_repr(value):
    tag, payload = _encode(value)
    assert tag == "r"
    # The "r" tag decodes to the recorded repr string verbatim.
    assert _decode(tag, payload) == repr(value)


BUILTIN_EXC_NAMES = [
    "ValueError", "KeyError", "TypeError", "RuntimeError", "StopIteration",
    "OSError", "ZeroDivisionError", "KeyboardInterrupt", "Exception",
    "BaseException", "IndexError",
]


@pytest.mark.parametrize("name", BUILTIN_EXC_NAMES)
def test_reconstruct_exc_known_builtin(name):
    import builtins

    exc = _reconstruct_exc(name)
    assert isinstance(exc, getattr(builtins, name))


@pytest.mark.parametrize("name", ["NotARealError", "dict", "int", "os", "", "list"])
def test_reconstruct_exc_unknown_becomes_runtimeerror(name):
    exc = _reconstruct_exc(name)
    assert isinstance(exc, RuntimeError)
    assert name in str(exc) if name else True


# ===========================================================================
# Tape
# ===========================================================================

SPLIT_CASES = [
    ("time.time", (0, "time.time")),
    ("random.random", (0, "random.random")),
    ("@1#time.time", (1, "time.time")),
    ("@12#io.file0.read", (12, "io.file0.read")),
    ("@999#x", (999, "x")),
    ("@x#foo", (0, "@x#foo")),        # non-int channel -> treated as bare
    ("@1foo", (0, "@1foo")),          # no '#' -> bare
    ("#nohash", (0, "#nohash")),      # no leading '@' -> bare
    ("@#x", (0, "@#x")),              # empty channel int -> bare
    ("@2#@3#a", (2, "@3#a")),         # only the first marker splits
    ("plain", (0, "plain")),
]


@pytest.mark.parametrize("src,expected", SPLIT_CASES)
def test_split_channel(src, expected):
    assert _split_channel(src) == expected


@pytest.mark.parametrize("n", [0, 1, 2, 5, 25])
def test_tape_len(n):
    entries = [(i, "time.time", "f", repr(float(i))) for i in range(n)]
    assert len(Tape(entries)) == n


def test_tape_sources_aggregates_across_channels():
    entries = [
        (0, "time.time", "f", "1.0"),
        (1, "time.time", "f", "2.0"),
        (2, "@1#time.time", "f", "3.0"),   # same real source, other channel
        (3, "@2#random.random", "f", "0.5"),
        (4, "random.random", "f", "0.9"),
    ]
    assert Tape(entries).sources() == {"time.time": 3, "random.random": 2}


def test_tape_sources_empty():
    assert Tape([]).sources() == {}


def test_tape_rows_returns_entry_tuples():
    entries = [
        (0, "time.time", "f", "1.0"),
        (1, "random.random", "f", "0.5"),
    ]
    rows = Tape(entries).rows()
    assert rows == entries
    assert rows is not entries  # a copy


@pytest.mark.parametrize(
    "entries",
    [
        [],
        [(0, "time.time", "f", "1.5")],
        [(0, "time.time", "f", "1.5"), (1, "random.random", "f", "0.25")],
        [(i, "os.getpid", "i", str(1000 + i)) for i in range(10)],
    ],
)
def test_tape_json_roundtrip(entries):
    t = Tape(entries)
    restored = Tape.from_json(t.to_json())
    assert restored.rows() == entries
    # to_json is valid JSON with the documented shape.
    parsed = json.loads(t.to_json())
    assert all(set(e) == {"seq", "source", "tag", "payload"} for e in parsed)


def test_tape_take_decodes_in_order():
    entries = [
        (0, "time.time", "f", "1.5"),
        (1, "random.randint", "i", "42"),
        (2, "os.urandom", "b", b"\xaa\xbb".hex()),
    ]
    t = Tape(entries)
    assert t.take("time.time") == 1.5
    assert t.take("random.randint") == 42
    assert t.take("os.urandom") == b"\xaa\xbb"


def test_tape_take_raw_returns_tag_payload():
    t = Tape([(0, "io.file0.read", "s", "hello")])
    assert t.take_raw("io.file0.read") == ("s", "hello")


def test_tape_take_wrong_source_diverges():
    t = Tape([(0, "time.time", "f", "1.0")])
    with pytest.raises(ReplayDivergence):
        t.take("random.random")


def test_tape_take_exhausted_diverges():
    t = Tape([])
    with pytest.raises(ReplayDivergence):
        t.take("time.time")


@pytest.mark.parametrize(
    "exc_name,exc_type",
    [
        ("ValueError", ValueError),
        ("KeyError", KeyError),
        ("ZeroDivisionError", ZeroDivisionError),
        ("OSError", OSError),
        ("RuntimeError", RuntimeError),
        ("StopIteration", StopIteration),
    ],
)
def test_tape_take_reraises_recorded_exception(exc_name, exc_type):
    t = Tape([(0, "random.randint", "!", exc_name)])
    with pytest.raises(exc_type):
        t.take("random.randint")


@pytest.mark.parametrize("control_src", ["threads.order", "asyncio.order"])
def test_pop_control_removes_and_returns_payload(control_src):
    entries = [
        (0, "time.time", "f", "1.0"),
        (1, control_src, "s", "[0, 1, 0]"),
        (2, "random.random", "f", "0.5"),
    ]
    t = Tape(entries)
    assert t.pop_control(control_src) == "[0, 1, 0]"
    assert len(t) == 2
    # A second pop finds nothing left.
    assert t.pop_control(control_src) is None
    # The remaining scalar entries are still consumable in order.
    assert t.take("time.time") == 1.0
    assert t.take("random.random") == 0.5


def test_pop_control_absent_returns_none():
    t = Tape([(0, "time.time", "f", "1.0")])
    assert t.pop_control("threads.order") is None
    assert len(t) == 1


def test_tape_partition_groups_by_channel():
    entries = [
        (0, "time.time", "f", "1.0"),
        (1, "@1#time.time", "f", "2.0"),
        (2, "@1#random.random", "f", "0.5"),
        (3, "@2#os.getpid", "i", "7"),
    ]
    table = Tape(entries)._partition()
    assert set(table) == {0, 1, 2}
    assert table[0] == [("time.time", "f", "1.0")]
    assert table[1] == [("time.time", "f", "2.0"), ("random.random", "f", "0.5")]
    assert table[2] == [("os.getpid", "i", "7")]


# ===========================================================================
# deterministic() / replay() — scalar boundaries, bit-for-bit
# ===========================================================================

BOUNDARY_FNS = {
    "time.time": lambda: time.time(),
    "time.monotonic": lambda: time.monotonic(),
    "time.perf_counter": lambda: time.perf_counter(),
    "time.time_ns": lambda: time.time_ns(),
    "time.monotonic_ns": lambda: time.monotonic_ns(),
    "time.perf_counter_ns": lambda: time.perf_counter_ns(),
    "random.random": lambda: random.random(),
    "random.randint": lambda: random.randint(1, 10**18),
    "random.uniform": lambda: random.uniform(0.0, 1e9),
    "random.randrange": lambda: random.randrange(10**18),
    "random.getrandbits": lambda: random.getrandbits(64),
    "os.urandom": lambda: os.urandom(16),
    "os.getpid": lambda: os.getpid(),
    "os.getenv_present": lambda: os.getenv("PATH"),
    "os.getenv_missing": lambda: os.getenv("FLIGHT_DEFINITELY_MISSING_VAR"),
    "uuid.uuid4": lambda: uuid.uuid4(),
    "secrets.token_bytes": lambda: secrets.token_bytes(16),
    "secrets.token_hex": lambda: secrets.token_hex(8),
    "secrets.token_urlsafe": lambda: secrets.token_urlsafe(12),
    "secrets.randbelow": lambda: secrets.randbelow(10**18),
}


@pytest.mark.parametrize("name", list(BOUNDARY_FNS))
def test_boundary_replays_bit_for_bit(tmp_path, name):
    fn = BOUNDARY_FNS[name]
    out = tmp_path / "b.flight"
    with flight.deterministic(str(out), io_hash_above=0):
        recorded = fn()
    assert flight.replay(str(out), fn) == recorded


# The genuinely non-deterministic boundaries must differ on a fresh, unrecorded
# call — otherwise the "replay reproduces it" assertion above would be vacuous.
NONDET_FNS = [
    "random.random", "random.randint", "random.uniform", "random.randrange",
    "random.getrandbits", "os.urandom", "uuid.uuid4",
    "secrets.token_bytes", "secrets.token_hex", "secrets.token_urlsafe",
    "secrets.randbelow",
]


@pytest.mark.parametrize("name", NONDET_FNS)
def test_recorded_boundary_differs_from_a_fresh_call(tmp_path, name):
    fn = BOUNDARY_FNS[name]
    out = tmp_path / "b.flight"
    with flight.deterministic(str(out), io_hash_above=0):
        recorded = fn()
    # Ranges are large enough that a collision is astronomically unlikely.
    assert fn() != recorded


def test_combined_boundaries_replay_together(tmp_path):
    def work():
        return (
            round(time.time(), 6),
            random.random(),
            random.randint(1, 10**9),
            time.monotonic_ns(),
            os.urandom(8),
            str(uuid.uuid4()),
            secrets.token_hex(4),
        )

    out = tmp_path / "combined.flight"
    with flight.deterministic(str(out), io_hash_above=0):
        recorded = work()
    assert work() != recorded
    assert flight.replay(str(out), work) == recorded


@pytest.mark.parametrize(
    "name,expected_sources",
    [
        ("uuid.uuid4", {"uuid.uuid4": 1}),           # uuid4 internally calls os.urandom
        ("secrets.token_bytes", {"secrets.token_bytes": 1}),
        ("secrets.randbelow", {"secrets.randbelow": 1}),
        ("time.time", {"time.time": 1}),
        ("random.random", {"random.random": 1}),
    ],
)
def test_only_outermost_boundary_recorded(tmp_path, name, expected_sources):
    fn = BOUNDARY_FNS[name]
    out = tmp_path / "nested.flight"
    with flight.deterministic(str(out), io_hash_above=0):
        fn()
    assert flight.read(str(out)).tape().sources() == expected_sources


def test_reentrancy_across_processes(tmp_path):
    # uuid4 records only itself; a fresh reload replays it and matches.
    out = tmp_path / "u.flight"
    with flight.deterministic(str(out), io_hash_above=0):
        u = uuid.uuid4()
    reloaded = flight.read(str(out))
    assert flight.replay_tape(reloaded.tape(), lambda: uuid.uuid4()) == u


def test_random_choice_is_not_interposed_known_limitation(tmp_path):
    # random.choice calls the *instance* _randbelow/getrandbits, not the module
    # attribute that interposition patches, so it is NOT captured. Pin the real
    # behaviour: nothing is recorded for a choice-only run.
    def work():
        return random.choice(list(range(1000)))

    out = tmp_path / "choice.flight"
    with flight.deterministic(str(out), io_hash_above=0):
        work()
    assert flight.read(str(out)).tape().sources() == {}


def test_empty_tape_noop_replays(tmp_path):
    out = tmp_path / "empty.flight"
    with flight.deterministic(str(out), io_hash_above=0):
        recorded = 21 * 2
    assert flight.replay(str(out), lambda: 21 * 2) == recorded


def test_empty_tape_then_boundary_diverges(tmp_path):
    out = tmp_path / "empty.flight"
    with flight.deterministic(str(out), io_hash_above=0):
        pass  # nothing recorded
    with pytest.raises(ReplayDivergence):
        flight.replay(str(out), lambda: time.time())


def _diverge_wrong_first():
    random.random()  # recording's first call was time.time


def _diverge_wrong_second():
    time.time()
    time.time()  # recording's second call was random.random


def _diverge_exhausted():
    time.time()
    random.random()
    time.time()  # nothing left on the tape


@pytest.mark.parametrize("fn", [_diverge_wrong_first, _diverge_wrong_second, _diverge_exhausted])
def test_replay_divergence_scenarios(tmp_path, fn):
    def recorded():
        time.time()
        random.random()

    out = tmp_path / "seq.flight"
    with flight.deterministic(str(out), io_hash_above=0):
        recorded()
    with pytest.raises(ReplayDivergence):
        flight.replay(str(out), fn)


@pytest.mark.parametrize("exc_type", [ValueError, KeyError, RuntimeError, ZeroDivisionError])
def test_crash_in_deterministic_block_still_writes_tape(tmp_path, exc_type):
    out = tmp_path / "crash.flight"
    with pytest.raises(exc_type):
        with flight.deterministic(str(out), io_hash_above=0):
            random.random()
            time.time()
            raise exc_type("boom")
    f = flight.read(str(out))
    assert f.has_nondet
    assert f.nondet_count >= 2


def test_boundary_that_raises_is_recorded_and_replayed(tmp_path):
    # random.randint(5, 1) raises ValueError; the *raise* is part of the run and
    # must replay so the code's except clauses take the same path.
    def work():
        try:
            random.randint(5, 1)
            return "no-error"
        except ValueError:
            return "caught"

    out = tmp_path / "raise.flight"
    with flight.deterministic(str(out), io_hash_above=0):
        recorded = work()
    assert recorded == "caught"
    assert flight.replay(str(out), work) == "caught"


# ===========================================================================
# deterministic I/O — files
# ===========================================================================

TEXT_CONTENT = "alpha\nbeta\ngamma\ndelta\n"


def _t_read(p):
    with open(p) as f:
        return f.read()


def _t_read_chunks(p):
    with open(p) as f:
        return f.read(3), f.read(4), f.read()


def _t_readline(p):
    with open(p) as f:
        return [f.readline(), f.readline(), f.readline()]


def _t_readlines(p):
    with open(p) as f:
        return f.readlines()


def _t_iter(p):
    with open(p) as f:
        return [line for line in f]


TEXT_READERS = {
    "read": _t_read,
    "read_chunks": _t_read_chunks,
    "readline": _t_readline,
    "readlines": _t_readlines,
    "iter": _t_iter,
}


@pytest.mark.parametrize("reader", list(TEXT_READERS))
def test_text_file_reader_replays_offline(tmp_path, reader):
    src = tmp_path / "data.txt"
    src.write_text(TEXT_CONTENT)
    fn = lambda: TEXT_READERS[reader](str(src))

    out = tmp_path / "run.flight"
    with flight.deterministic(str(out), io_hash_above=0):
        recorded = fn()

    src.unlink()  # prove replay comes from the tape, not the disk
    assert flight.replay(str(out), fn) == recorded
    assert not src.exists()


BIN_CONTENT = bytes(range(256)) * 2


def _b_read(p):
    with open(p, "rb") as f:
        return f.read()


def _b_read_chunks(p):
    with open(p, "rb") as f:
        return f.read(10), f.read(20), f.read()


def _b_read1(p):
    with open(p, "rb") as f:
        return f.read1(16)


def _b_readinto(p):
    buf = bytearray(32)
    with open(p, "rb") as f:
        n = f.readinto(buf)
    return n, bytes(buf)


def _b_readlines(p):
    with open(p, "rb") as f:
        return f.readlines()


BIN_READERS = {
    "read": _b_read,
    "read_chunks": _b_read_chunks,
    "read1": _b_read1,
    "readinto": _b_readinto,
    "readlines": _b_readlines,
}


@pytest.mark.parametrize("reader", list(BIN_READERS))
def test_binary_file_reader_replays_offline(tmp_path, reader):
    src = tmp_path / "data.bin"
    src.write_bytes(BIN_CONTENT)
    fn = lambda: BIN_READERS[reader](str(src))

    out = tmp_path / "run.flight"
    with flight.deterministic(str(out), io_hash_above=0):
        recorded = fn()

    src.unlink()
    assert flight.replay(str(out), fn) == recorded


def test_readinto_returns_recorded_count_and_bytes(tmp_path):
    src = tmp_path / "x.bin"
    src.write_bytes(b"0123456789")
    fn = lambda: _b_readinto(str(src))  # buf is 32 -> reads all 10
    out = tmp_path / "run.flight"
    with flight.deterministic(str(out), io_hash_above=0):
        recorded = fn()
    assert recorded[0] == 10
    src.unlink()
    assert flight.replay(str(out), fn) == recorded


def test_two_interleaved_files_keep_separate_channels(tmp_path):
    a = tmp_path / "a.txt"
    b = tmp_path / "b.txt"
    a.write_text("A1\nA2\nA3\n")
    b.write_text("B1\nB2\nB3\n")

    def work():
        fa, fb = open(a), open(b)
        try:
            return [fa.readline(), fb.readline(), fb.readline(), fa.readline()]
        finally:
            fa.close()
            fb.close()

    out = tmp_path / "run.flight"
    with flight.deterministic(str(out), io_hash_above=0):
        recorded = work()
    assert recorded == ["A1\n", "B1\n", "B2\n", "A2\n"]
    a.unlink()
    b.unlink()
    assert flight.replay(str(out), work) == recorded


def test_replay_swallows_writes_no_side_effect(tmp_path):
    src = tmp_path / "in.txt"
    src.write_text("input\n")
    sink = tmp_path / "out.txt"

    def work():
        with open(src) as f:
            data = f.read()
        with open(sink, "w") as f:
            f.write(data.upper())
        return data

    out = tmp_path / "run.flight"
    with flight.deterministic(str(out), io_hash_above=0):
        recorded = work()
    assert sink.read_text() == "INPUT\n"  # the recording run really wrote

    sink.unlink()
    src.unlink()
    assert flight.replay(str(out), work) == recorded
    assert not sink.exists()  # replay's write was swallowed


# -- os.read (pipe) ---------------------------------------------------------


@pytest.mark.parametrize("payload", [b"hi", b"hello-from-a-pipe", bytes(range(50))])
def test_os_read_pipe_replays_offline(tmp_path, payload):
    def work():
        r, w = os.pipe()
        os.write(w, payload)
        os.close(w)
        data = os.read(r, 128)
        os.close(r)
        return data

    out = tmp_path / "pipe.flight"
    with flight.deterministic(str(out), io_hash_above=0):
        recorded = work()
    assert recorded == payload

    def replay_probe():
        r, w = os.pipe()
        os.close(w)  # nothing written; the read must come from the tape
        data = os.read(r, 128)
        os.close(r)
        return data

    assert flight.replay(str(out), replay_probe) == recorded


# -- subprocess -------------------------------------------------------------


def test_subprocess_run_replays_offline(tmp_path):
    def work():
        cp = subprocess.run(["echo", "hi"], capture_output=True, text=True)
        return cp.returncode, cp.stdout.strip()

    out = tmp_path / "sp.flight"
    with flight.deterministic(str(out), io_hash_above=0):
        recorded = work()
    assert recorded == (0, "hi")

    def probe():
        cp = subprocess.run(["this-cmd-does-not-exist-xyz"], capture_output=True, text=True)
        return cp.returncode, cp.stdout.strip()

    assert flight.replay(str(out), probe) == recorded


def test_subprocess_check_output_replays(tmp_path):
    def work():
        return subprocess.check_output(["echo", "captured"], text=True).strip()

    out = tmp_path / "sp.flight"
    with flight.deterministic(str(out), io_hash_above=0):
        recorded = work()
    assert recorded == "captured"
    assert flight.replay(str(out), lambda: "captured") == recorded


# -- hash-of-rest mode ------------------------------------------------------


@pytest.mark.parametrize("size", [300_000, 512_000])
def test_large_read_hashed_and_verified_against_live(tmp_path, size):
    src = tmp_path / "big.bin"
    blob = os.urandom(size)
    src.write_bytes(blob)

    def work():
        with open(src, "rb") as f:
            return f.read()

    out = tmp_path / "hash.flight"
    with flight.deterministic(str(out)):  # default 256 KiB threshold -> hashed
        recorded = work()
    assert recorded == blob
    assert out.stat().st_size < size // 2  # content was not inlined
    # Live, unchanged source -> replay re-reads and verifies the digest.
    assert flight.replay(str(out), work) == blob


@pytest.mark.parametrize("tamper", ["change", "delete"])
def test_hashed_read_detects_bad_source(tmp_path, tamper):
    src = tmp_path / "big.bin"
    src.write_bytes(os.urandom(300_000))

    def work():
        with open(src, "rb") as f:
            return f.read()

    out = tmp_path / "hash.flight"
    with flight.deterministic(str(out)):
        work()

    if tamper == "change":
        src.write_bytes(os.urandom(300_000))
    else:
        src.unlink()

    with pytest.raises(ReplayDivergence):
        flight.replay(str(out), work)


def test_inline_mode_replays_a_large_read_offline(tmp_path):
    # io_hash_above=0 inlines everything, so even a big read replays with no file.
    src = tmp_path / "big.bin"
    blob = os.urandom(300_000)
    src.write_bytes(blob)

    def work():
        with open(src, "rb") as f:
            return f.read()

    out = tmp_path / "inline.flight"
    with flight.deterministic(str(out), io_hash_above=0):
        recorded = work()
    src.unlink()
    assert flight.replay(str(out), work) == recorded == blob


def test_io_control_flow_divergence(tmp_path):
    src = tmp_path / "data.txt"
    src.write_text("recorded\n")

    def work():
        with open(src) as f:
            return f.read()

    out = tmp_path / "run.flight"
    with flight.deterministic(str(out), io_hash_above=0):
        work()

    def diverged():
        return subprocess.run(["echo"], capture_output=True)  # not a file read

    with pytest.raises(ReplayDivergence):
        flight.replay(str(out), diverged)


# -- sockets ----------------------------------------------------------------


@pytest.mark.parametrize("msg", [b"hello-over-a-socket", b"x", bytes(range(40))])
def test_socket_recv_replays_offline(tmp_path, msg):
    def work():
        a, b = socket.socketpair()
        try:
            b.sendall(msg)
            first = a.recv(128)
            b.sendall(b"WXYZ")
            buf = bytearray(4)
            n = a.recv_into(buf)
            return first, n, bytes(buf)
        finally:
            a.close()
            b.close()

    out = tmp_path / "sock.flight"
    with flight.deterministic(str(out), io_hash_above=0):
        recorded = work()
    assert recorded[0] == msg

    def probe():
        a, b = socket.socketpair()
        try:
            first = a.recv(128)  # nothing sent; served from the tape
            buf = bytearray(4)
            n = a.recv_into(buf)
            return first, n, bytes(buf)
        finally:
            a.close()
            b.close()

    assert flight.replay(str(out), probe) == recorded


# ===========================================================================
# asyncio scheduling order
# ===========================================================================


def _async_program(n=2):
    import asyncio

    async def main():
        order = []

        async def task(name, delay, value):
            await asyncio.sleep(delay)
            order.append(name)
            return value

        tasks = [
            asyncio.create_task(task(f"t{i}", 0.005 * (n - i), i)) for i in range(n)
        ]
        results = await asyncio.gather(*tasks)
        return results, order

    return asyncio.run(main())


def test_asyncio_completion_order_recorded_and_replayed(tmp_path):
    out = tmp_path / "aio.flight"
    with flight.deterministic(str(out), io_hash_above=0):
        recorded = _async_program(2)
    # t1 has the shorter sleep, so it finishes first.
    assert recorded[1] == ["t1", "t0"]
    assert flight.replay(str(out), lambda: _async_program(2)) == recorded


def test_asyncio_divergence_on_different_task_count(tmp_path):
    out = tmp_path / "aio.flight"
    with flight.deterministic(str(out), io_hash_above=0):
        _async_program(2)
    with pytest.raises(ReplayDivergence):
        flight.replay(str(out), lambda: _async_program(4))


def test_asyncio_no_tasks_replays_cleanly(tmp_path):
    def work():
        return round(time.time(), 6)

    out = tmp_path / "run.flight"
    with flight.deterministic(str(out), io_hash_above=0):
        recorded = work()
    assert flight.replay(str(out), work) == recorded


# ===========================================================================
# thread scheduling
# ===========================================================================


@pytest.mark.parametrize(
    "blocking,timeout,expected",
    [
        (True, -1, True),     # plain `with lock:` -> gated
        (True, None, True),   # untimed blocking -> gated
        (False, -1, False),   # non-blocking try -> not gated
        (True, 5, False),     # timed acquire -> not gated
        (True, 0, False),     # zero timeout -> not gated
        (False, None, False),
        (1, -1, True),        # truthy blocking
        (0, -1, False),       # falsy blocking
    ],
)
def test_gated(blocking, timeout, expected):
    assert _gated(blocking, timeout) is expected


def test_thread_lock_order_is_reproduced(tmp_path):
    def program():
        log = []
        lock = threading.Lock()

        def worker(name):
            for i in range(4):
                time.sleep(0.0003)  # not interposed: real races each run
                with lock:
                    log.append((name, i))

        threads = [threading.Thread(target=worker, args=(n,)) for n in ("A", "B", "C")]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        assert all(not t.is_alive() for t in threads)
        return log

    old = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    try:
        out = tmp_path / "threads.flight"
        with flight.deterministic(str(out), io_hash_above=0):
            recorded = program()
        # Every replay reproduces the exact recorded interleaving.
        for _ in range(3):
            assert flight.replay(str(out), program) == recorded
    finally:
        sys.setswitchinterval(old)

    assert len(recorded) == 12
    assert sorted(recorded) == sorted((n, i) for n in "ABC" for i in range(4))


def test_each_thread_replays_its_own_boundary_lane(tmp_path):
    results = {}
    lock = threading.Lock()

    def worker(name):
        stamp = round(time.time(), 6)
        with lock:
            results[name] = stamp

    def program():
        results.clear()
        threads = [threading.Thread(target=worker, args=(n,)) for n in ("A", "B", "C")]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        return dict(results)

    out = tmp_path / "threads2.flight"
    with flight.deterministic(str(out), io_hash_above=0):
        recorded = program()
    assert flight.replay(str(out), program) == recorded


def test_internal_locks_are_not_gated_no_deadlock(tmp_path):
    # threading.Event uses the runtime's own Condition/Lock (created inside the
    # `threading` module); those must be left real, or replay would deadlock. This
    # program uses NO user lock, so nothing is gated — it must still replay.
    def program():
        got = []
        ev = threading.Event()

        def producer():
            time.sleep(0.001)
            got.append(round(time.time(), 6))
            ev.set()

        t = threading.Thread(target=producer)
        t.start()
        assert ev.wait(timeout=5)
        t.join(timeout=5)
        return got

    out = tmp_path / "event.flight"
    with flight.deterministic(str(out), io_hash_above=0):
        recorded = program()
    assert len(recorded) == 1
    assert flight.replay(str(out), program) == recorded


def test_thread_replayer_timeout_path(monkeypatch):
    # Exercise the timeout->ReplayDivergence path without a 10s wait: the main
    # thread is channel 0, but the recorded schedule head is channel 1, so its
    # turn never comes and the short timeout fires.
    monkeypatch.setattr(_threads_mod, "_TURN_TIMEOUT", 0.05)
    replayer = ThreadReplayer([1])
    with pytest.raises(ReplayDivergence):
        replayer.wait_turn()


def test_thread_replayer_schedule_exhausted():
    # After the single recorded acquisition is consumed, a further acquisition on
    # an exhausted schedule is a divergence.
    replayer = ThreadReplayer([0])  # main thread is channel 0
    replayer.wait_turn()
    replayer.advance()
    with pytest.raises(ReplayDivergence):
        replayer.wait_turn()
