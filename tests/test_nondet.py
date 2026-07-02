"""Phase 3, rung 2 — deterministic record & replay."""

from __future__ import annotations

import os
import random
import time
import uuid

import pytest

import flight
from flight._nondet import Tape, _decode, _encode


# -- codec ------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [0, 1, -5, 2**70, 3.14, float("inf"), "hello", "", b"\x00\xff", True, False, None,
     {"a": 1, "b": "x"}],
)
def test_codec_roundtrips(value):
    tag, payload = _encode(value)
    got = _decode(tag, payload)
    if isinstance(value, float) and value != value:  # nan
        assert got != got
    else:
        assert got == value


def test_codec_uuid():
    u = uuid.uuid4()
    tag, payload = _encode(u)
    assert _decode(tag, payload) == u


# -- record + replay --------------------------------------------------------


def _work():
    return (
        round(time.time(), 6),
        random.random(),
        random.randint(1, 10**9),
        time.monotonic_ns(),
        os.urandom(8),
        str(uuid.uuid4()),
    )


def test_replay_is_bit_for_bit_deterministic(tmp_path):
    out = tmp_path / "run.flight"
    with flight.deterministic(out):
        original = _work()
    # A normal second call differs (real non-determinism)...
    assert _work() != original
    # ...but replay reproduces the original exactly.
    replayed = flight.replay(out, _work)
    assert replayed == original


def test_replay_across_processes_via_persisted_file(tmp_path):
    out = tmp_path / "run.flight"
    with flight.deterministic(out):
        original = _work()
    # Reload the file fresh (as another process would) and replay.
    reloaded = flight.read(out)
    assert reloaded.has_nondet
    assert reloaded.nondet_count > 0
    replayed = flight.replay_tape(reloaded.tape(), _work)
    assert replayed == original


def test_replay_divergence_on_different_call_pattern(tmp_path):
    out = tmp_path / "run.flight"
    with flight.deterministic(out):
        _work()

    def different():
        return random.random()  # first call is random, not time.time

    with pytest.raises(flight.ReplayDivergence):
        flight.replay(out, different)


def test_replay_divergence_when_tape_exhausted(tmp_path):
    out = tmp_path / "run.flight"
    with flight.deterministic(out):
        time.time()  # record exactly one value

    def greedy():
        time.time()
        time.time()  # second call has nothing left on the tape

    with pytest.raises(flight.ReplayDivergence):
        flight.replay(out, greedy)


def test_nested_boundary_records_only_outermost(tmp_path):
    # uuid.uuid4() internally calls os.urandom; only uuid4 should be on the tape.
    out = tmp_path / "run.flight"
    with flight.deterministic(out):
        u = uuid.uuid4()
    tape = flight.read(out).tape()
    assert tape.sources() == {"uuid.uuid4": 1}
    # Call through the module attribute (as real code does) so interposition
    # applies; passing the bound original would bypass the patch.
    assert flight.replay(out, lambda: uuid.uuid4()) == u


def test_tape_json_roundtrip(tmp_path):
    out = tmp_path / "run.flight"
    with flight.deterministic(out):
        random.random()
        random.random()
    tape = flight.read(out).tape()
    restored = Tape.from_json(tape.to_json())
    assert len(restored) == 2
    assert restored.take("random.random") == flight.read(out).tape().take("random.random")


def test_deterministic_writes_a_readable_flight(tmp_path):
    out = tmp_path / "run.flight"
    with flight.deterministic(out):
        random.random()
    f = flight.read(out)
    assert "NONDET" in f.blocks
    assert not f.partial


def test_exception_in_deterministic_block_still_writes(tmp_path):
    out = tmp_path / "run.flight"
    with pytest.raises(ValueError):
        with flight.deterministic(out):
            random.random()
            raise ValueError("boom")
    assert flight.read(out).has_nondet
