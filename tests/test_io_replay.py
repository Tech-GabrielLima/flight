"""Phase 4a — deterministic I/O + asyncio replay.

Records what the code read (files, os.read pipes, subprocess output) and the
asyncio task-completion order, then replays it *offline* — the source files are
deleted before replay to prove the run repeats from the tape, not the world.
"""

from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import sys
import threading
import time

import pytest

import flight
from flight._nondet import ReplayDivergence


# -- files ------------------------------------------------------------------


def test_text_file_read_replays_offline(tmp_path):
    src = tmp_path / "data.txt"
    src.write_text("alpha\nbeta\ngamma\n")

    def work():
        with open(src) as f:
            whole = f.read()
        with open(src) as f:
            lines = [ln.strip() for ln in f]
        return whole, lines

    out = tmp_path / "run.flight"
    with flight.deterministic(str(out), io_hash_above=0):
        original = work()

    src.unlink()  # the world is gone; replay must come from the tape
    assert flight.replay(str(out), work) == original


def test_binary_file_read_replays_offline(tmp_path):
    src = tmp_path / "data.bin"
    blob = bytes(range(256)) * 4
    src.write_bytes(blob)

    def work():
        with open(src, "rb") as f:
            return f.read(10), f.read()

    out = tmp_path / "run.flight"
    with flight.deterministic(str(out), io_hash_above=0):
        original = work()
    assert original[0] == blob[:10]

    src.unlink()
    assert flight.replay(str(out), work) == original


def test_readinto_replays(tmp_path):
    src = tmp_path / "data.bin"
    src.write_bytes(b"0123456789")

    def work():
        buf = bytearray(4)
        with open(src, "rb") as f:
            n = f.readinto(buf)
        return n, bytes(buf)

    out = tmp_path / "run.flight"
    with flight.deterministic(str(out), io_hash_above=0):
        original = work()
    assert original == (4, b"0123")

    src.unlink()
    assert flight.replay(str(out), work) == original


def test_two_interleaved_files_dont_cross_wires(tmp_path):
    a = tmp_path / "a.txt"
    b = tmp_path / "b.txt"
    a.write_text("A1\nA2\n")
    b.write_text("B1\nB2\n")

    def work():
        fa, fb = open(a), open(b)
        try:
            # interleave reads across the two channels
            return [fa.readline(), fb.readline(), fa.readline(), fb.readline()]
        finally:
            fa.close()
            fb.close()

    out = tmp_path / "run.flight"
    with flight.deterministic(str(out), io_hash_above=0):
        original = work()
    assert original == ["A1\n", "B1\n", "A2\n", "B2\n"]

    a.unlink()
    b.unlink()
    assert flight.replay(str(out), work) == original


# -- os.read (pipes) --------------------------------------------------------


def test_os_read_pipe_replays_offline(tmp_path):
    def work():
        r, w = os.pipe()
        os.write(w, b"hello-from-a-pipe")
        os.close(w)
        data = os.read(r, 64)
        os.close(r)
        return data

    out = tmp_path / "run.flight"
    with flight.deterministic(str(out), io_hash_above=0):
        original = work()
    assert original == b"hello-from-a-pipe"

    # Replay in a wrapper whose pipe is empty — the read comes from the tape.
    def replay_probe():
        r, w = os.pipe()
        os.close(w)  # nothing written
        data = os.read(r, 64)
        os.close(r)
        return data

    assert flight.replay(str(out), replay_probe) == original


# -- subprocess -------------------------------------------------------------


def test_subprocess_run_replays_offline(tmp_path):
    def work():
        cp = subprocess.run(["echo", "hi"], capture_output=True, text=True)
        return cp.returncode, cp.stdout.strip()

    out = tmp_path / "run.flight"
    with flight.deterministic(str(out), io_hash_above=0):
        original = work()
    assert original == (0, "hi")

    # Replay calling a command that does NOT exist proves it never runs.
    def replay_probe():
        cp = subprocess.run(["this-command-does-not-exist-xyz"], capture_output=True, text=True)
        return cp.returncode, cp.stdout.strip()

    assert flight.replay(str(out), replay_probe) == original


def test_subprocess_check_output_replays(tmp_path):
    def work():
        return subprocess.check_output(["echo", "captured"], text=True).strip()

    out = tmp_path / "run.flight"
    with flight.deterministic(str(out), io_hash_above=0):
        original = work()
    assert original == "captured"
    assert flight.replay(str(out), lambda: "captured") == original


# -- hash-of-rest mode ------------------------------------------------------


def test_large_read_is_hashed_and_verified_against_live(tmp_path):
    src = tmp_path / "big.bin"
    blob = os.urandom(300_000)
    src.write_bytes(blob)

    def work():
        with open(src, "rb") as f:
            return f.read()

    out = tmp_path / "run.flight"
    with flight.deterministic(str(out)):  # default 256 KiB threshold -> hashed
        original = work()
    assert original == blob
    # The file did not inline 300 KB of content.
    assert out.stat().st_size < 50_000

    # Live file present and unchanged -> replay returns the live bytes.
    assert flight.replay(str(out), work) == blob


def test_hashed_read_detects_a_changed_source(tmp_path):
    src = tmp_path / "big.bin"
    src.write_bytes(os.urandom(300_000))

    def work():
        with open(src, "rb") as f:
            return f.read()

    out = tmp_path / "run.flight"
    with flight.deterministic(str(out)):
        work()

    src.write_bytes(os.urandom(300_000))  # tamper with the live source
    with pytest.raises(ReplayDivergence):
        flight.replay(str(out), work)


def test_hashed_read_refuses_when_source_is_gone(tmp_path):
    src = tmp_path / "big.bin"
    src.write_bytes(os.urandom(300_000))

    def work():
        with open(src, "rb") as f:
            return f.read()

    out = tmp_path / "run.flight"
    with flight.deterministic(str(out)):
        work()

    src.unlink()
    with pytest.raises(ReplayDivergence):
        flight.replay(str(out), work)


# -- divergence -------------------------------------------------------------


def test_control_flow_divergence_points_at_the_step(tmp_path):
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


# -- writes on replay are swallowed (no real side effects) ------------------


def test_replay_does_not_write_to_disk(tmp_path):
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
        original = work()
    assert sink.read_text() == "INPUT\n"  # the record run really wrote

    sink.unlink()
    src.unlink()
    assert flight.replay(str(out), work) == original
    assert not sink.exists()  # replay swallowed the write — no side effect


# -- asyncio scheduling order -----------------------------------------------


def _async_program():
    async def main():
        order = []

        async def task(name, delay, value):
            await asyncio.sleep(delay)
            order.append(name)
            return value

        a = asyncio.create_task(task("a", 0.03, 1))
        b = asyncio.create_task(task("b", 0.01, 2))
        results = await asyncio.gather(a, b)
        return results, order

    return asyncio.run(main())


def test_asyncio_completion_order_recorded_and_replayed(tmp_path):
    out = tmp_path / "aio.flight"
    with flight.deterministic(str(out), io_hash_above=0):
        original = _async_program()
    # 'b' has the shorter sleep, so it completes first.
    assert original[1] == ["b", "a"]

    replayed = flight.replay(str(out), _async_program)
    assert replayed == original


def test_asyncio_no_tasks_records_nothing_extra(tmp_path):
    # A deterministic run that never touches asyncio must still replay cleanly.
    def work():
        return round(time.time(), 6)

    out = tmp_path / "run.flight"
    with flight.deterministic(str(out), io_hash_above=0):
        original = work()
    assert flight.replay(str(out), work) == original


# -- sockets ----------------------------------------------------------------


def test_socketpair_recv_replays_offline(tmp_path):
    def work():
        a, b = socket.socketpair()
        try:
            b.sendall(b"hello-over-a-socket")
            first = a.recv(64)
            b.sendall(b"WXYZ")
            buf = bytearray(4)
            n = a.recv_into(buf)
            return first, n, bytes(buf)
        finally:
            a.close()
            b.close()

    out = tmp_path / "sock.flight"
    with flight.deterministic(str(out), io_hash_above=0):
        original = work()
    assert original[0] == b"hello-over-a-socket"

    # Replay through a socket that nothing ever writes to — data is from the tape.
    def replay_probe():
        a, b = socket.socketpair()
        try:
            first = a.recv(64)
            buf = bytearray(4)
            n = a.recv_into(buf)
            return first, n, bytes(buf)
        finally:
            a.close()
            b.close()

    assert flight.replay(str(out), replay_probe) == original


# -- thread scheduling (lock-acquisition order) -----------------------------


def _contended_program():
    """Three threads racing to append to a shared list under one lock. Which
    thread appends when varies run to run (the classic flaky-order bug); the
    sleeps make the raw interleaving genuinely non-deterministic."""
    log = []
    lock = threading.Lock()

    def worker(name):
        for i in range(6):
            time.sleep(0.0005)  # not interposed: real races on every run
            with lock:
                log.append((name, i))

    threads = [threading.Thread(target=worker, args=(n,)) for n in ("A", "B", "C")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return log


def test_thread_lock_order_is_reproduced(tmp_path):
    old = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)  # encourage real interleaving while recording
    try:
        out = tmp_path / "threads.flight"
        with flight.deterministic(str(out), io_hash_above=0):
            recorded = _contended_program()

        # Every replay reproduces the exact recorded interleaving, even though
        # the raw thread race (the sleeps) differs each time.
        for _ in range(4):
            assert flight.replay(str(out), _contended_program) == recorded
    finally:
        sys.setswitchinterval(old)
    # sanity: all 18 appends are present
    assert len(recorded) == 18
    assert sorted(recorded) == sorted((n, i) for n in "ABC" for i in range(6))


def test_threads_each_replay_their_own_boundary_calls(tmp_path):
    """Each worker reads the clock on its own lane; replay feeds each thread its
    own recorded values (per-thread cursors), and the lock order is reproduced."""
    results = {}
    lock = threading.Lock()

    def worker(name):
        stamp = round(time.time(), 6)
        with lock:
            results[name] = stamp

    def program():
        results.clear()
        threads = [threading.Thread(target=worker, args=(n,)) for n in ("A", "B")]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        return dict(results)

    out = tmp_path / "threads2.flight"
    with flight.deterministic(str(out), io_hash_above=0):
        recorded = program()

    replayed = flight.replay(str(out), program)
    assert replayed == recorded  # each thread's recorded clock value came back
