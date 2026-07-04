"""Coverage-completion tests for the recording / replay-engine modules.

Targets the specific uncovered lines in `_io`, `_record`, `_nondet`, `_threads`
and `_asyncio`. Each test drives a real code path (record then replay, a
hand-built `Tape`, or a direct object under test). Everything is deterministic
and fast; the autouse `_clean_flight` fixture uninstalls flight between tests.
"""

from __future__ import annotations

import io
import json
import os
import socket
import subprocess
import sys
import threading

import pytest

import flight
from flight._nondet import ReplayDivergence, Tape


# ===========================================================================
# _io.py
# ===========================================================================


def _tape(*rows) -> Tape:
    """Build a Tape from ``(source, tag, payload)`` rows in call order."""
    return Tape((i, src, tag, payload) for i, (src, tag, payload) in enumerate(rows))


# -- _RecordingFile (record side) -------------------------------------------


def test_recording_file_all_read_methods_and_setattr(tmp_path):
    """Drives read/read1/readline/readinto/readinto1 + iteration + readlines
    through the recording wrapper, plus __setattr__ (line 73)."""
    src = tmp_path / "data.bin"
    src.write_bytes(b"L1\nL2\nL3\n")

    def work():
        results = []
        with open(src, "rb") as f:
            # __setattr__ (73): setting an attribute is delegated to the raw file
            try:
                f.custom_flight_attr = 1  # read-only file -> AttributeError, still runs 73
            except AttributeError:
                pass
            results.append(f.read(2))          # read
            results.append(f.read1(1))         # read1
            results.append(f.readline())       # readline
            buf = bytearray(2)
            results.append((f.readinto(buf), bytes(buf)))     # readinto (90-95)
            buf2 = bytearray(1)
            results.append((f.readinto1(buf2), bytes(buf2)))  # readinto1 (114)
        with open(src, "rb") as f2:
            results.append(f2.readlines())     # readlines
        with open(src, "rb") as f3:
            results.append([ln for ln in f3])  # __iter__/__next__
        return results

    out = tmp_path / "rec.flight"
    with flight.deterministic(str(out), io_hash_above=0):
        original = work()
    src.unlink()
    assert flight.replay(str(out), work) == original


def test_recording_file_read_that_raises_is_recorded_and_replayed(tmp_path):
    """A read whose underlying call raises records a '!' entry (83-87) and the
    replay reconstructs the exception (192-193)."""
    src = tmp_path / "d.txt"
    src.write_text("hi")

    def work():
        with open(src) as f:
            try:
                f.read("not-an-int")  # TypeError inside the raw read -> 83-87
            except TypeError:
                return "caught"
        return "no"

    out = tmp_path / "rec.flight"
    with flight.deterministic(str(out), io_hash_above=0):
        assert work() == "caught"

    def replay_probe():
        with open(src) as fh:
            try:
                fh.read()  # served from the tape -> tag '!' -> reraises TypeError
            except TypeError:
                return "caught"
        return "no"

    src.unlink()
    assert flight.replay(str(out), replay_probe) == "caught"


# -- _ReplayFile (replay side), driven with hand-built tapes ----------------


def test_replayfile_pull_reraises_recorded_exception():
    """_pull tag '!' -> reconstructed exception (192-193)."""
    from flight._io import _ReplayFile

    rf = _ReplayFile(_tape(("io.file0.read", "!", "ValueError")), 0, None)
    with pytest.raises(ValueError):
        rf.read()


def test_replayfile_hashed_read_without_open_args_diverges():
    """A hashed read on a channel whose open args were not recorded needs the
    original file but has no way to reach it -> ReplayDivergence (175)."""
    from flight._io import _ReplayFile

    rf = _ReplayFile(_tape(("io.file0.read", "h", "b:10:abcdef")), 0, None)
    with pytest.raises(ReplayDivergence):
        rf.read()


def test_replayfile_readinto_and_readinto1_and_readlines_and_iter():
    """readinto (207-210, 223), readinto1 (226), readlines (228-235) and
    iteration served from tapes."""
    from flight._io import _ReplayFile

    rf = _ReplayFile(_tape(("io.file0.readinto", "b", b"abcd".hex())), 0, None)
    buf = bytearray(4)
    assert rf.readinto(buf) == 4
    assert bytes(buf) == b"abcd"

    rf2 = _ReplayFile(_tape(("io.file0.readinto", "b", b"zz".hex())), 0, None)
    buf2 = bytearray(2)
    assert rf2.readinto1(buf2) == 2  # 226
    assert bytes(buf2) == b"zz"

    rf3 = _ReplayFile(
        _tape(
            ("io.file0.readline", "s", "one\n"),
            ("io.file0.readline", "s", "two\n"),
            ("io.file0.readline", "s", ""),
        ),
        0,
        None,
    )
    assert rf3.readlines() == ["one\n", "two\n"]  # 228-235

    rf4 = _ReplayFile(
        _tape(
            ("io.file0.readline", "s", "x\n"),
            ("io.file0.readline", "s", ""),
        ),
        0,
        None,
    )
    assert list(rf4) == ["x\n"]  # __iter__/__next__


def test_replayfile_write_sink_methods():
    """The swallowed-side-effect methods on a replayed file: writelines (251-252),
    seek (255), tell (258), flush (261), truncate (264), fileno (272)."""
    from flight._io import _ReplayFile

    rf = _ReplayFile(None, 7, None)
    assert rf.write(b"xyz") == 3
    assert rf.writelines(["a", "b", "c"]) is None  # 251-252
    assert rf.seek(5) == 5  # 255
    assert rf.tell() == 0  # 258
    assert rf.flush() is None  # 261
    assert rf.truncate() == 0  # 264
    with pytest.raises(io.UnsupportedOperation):
        rf.fileno()  # 272
    with _ReplayFile(None, 0, None) as g:
        assert g.write(b"") == 0
    rf.close()
    assert rf.closed


# -- os.read / subprocess / socket: record error + replay branches ----------


def test_os_read_error_recorded_and_replayed(tmp_path):
    """os.read raising is recorded (335-339) and reconstructed on replay (453)."""
    def work():
        try:
            os.read(-1, 10)  # bad fd -> OSError -> 335-339
        except OSError:
            return "caught"
        return "no"

    out = tmp_path / "osread.flight"
    with flight.deterministic(str(out), io_hash_above=0):
        assert work() == "caught"

    with pytest.raises(OSError):
        flight.replay(str(out), lambda: os.read(0, 10))  # 453


def test_os_read_hashed_cannot_replay_offline(tmp_path):
    """A large os.read recorded in hashed mode can't be replayed offline (455)."""
    def work():
        r, w = os.pipe()
        os.write(w, b"0123456789")
        os.close(w)
        data = os.read(r, 64)
        os.close(r)
        return data

    out = tmp_path / "osread_h.flight"
    with flight.deterministic(str(out), io_hash_above=4):  # 10 bytes > 4 -> hashed
        assert work() == b"0123456789"

    with pytest.raises(ReplayDivergence):
        flight.replay(str(out), lambda: os.read(0, 64))  # 455


def test_subprocess_error_recorded_and_replayed(tmp_path):
    """subprocess.run raising is recorded (355-359) and reconstructed (469)."""
    def work():
        try:
            subprocess.run(["definitely-not-real-xyzzy-cmd"])
        except FileNotFoundError:
            return "caught"
        return "no"

    out = tmp_path / "sub.flight"
    with flight.deterministic(str(out), io_hash_above=0):
        assert work() == "caught"

    with pytest.raises(FileNotFoundError):
        flight.replay(str(out), lambda: subprocess.run(["x"]))  # 469


def test_recv_error_recorded_and_replayed(tmp_path):
    """socket.recv raising is recorded (372-376) and reconstructed (478)."""
    def work():
        s = socket.socket()
        s.close()
        try:
            s.recv(10)  # closed fd -> OSError -> 372-376
        except OSError:
            return "caught"
        return "no"

    out = tmp_path / "recv.flight"
    with flight.deterministic(str(out), io_hash_above=0):
        assert work() == "caught"

    def probe():
        s = socket.socket()
        s.close()
        return s.recv(10)

    with pytest.raises(OSError):
        flight.replay(str(out), probe)  # 478


def test_recv_hashed_cannot_replay_offline(tmp_path):
    """A large recv recorded in hashed mode can't be replayed offline (480)."""
    def work():
        a, b = socket.socketpair()
        try:
            b.sendall(b"hello")
            return a.recv(64)
        finally:
            a.close()
            b.close()

    out = tmp_path / "recv_h.flight"
    with flight.deterministic(str(out), io_hash_above=4):  # 5 bytes > 4 -> hashed
        assert work() == b"hello"

    def probe():
        a, b = socket.socketpair()
        try:
            return a.recv(64)
        finally:
            a.close()
            b.close()

    with pytest.raises(ReplayDivergence):
        flight.replay(str(out), probe)  # 480


def test_recv_into_error_recorded_and_replayed(tmp_path):
    """socket.recv_into raising is recorded (390-394) and reconstructed (492)."""
    def work():
        s = socket.socket()
        s.close()
        try:
            s.recv_into(bytearray(4))  # closed fd -> OSError -> 390-394
        except OSError:
            return "caught"
        return "no"

    out = tmp_path / "recvinto.flight"
    with flight.deterministic(str(out), io_hash_above=0):
        assert work() == "caught"

    def probe():
        s = socket.socket()
        s.close()
        return s.recv_into(bytearray(4))

    with pytest.raises(OSError):
        flight.replay(str(out), probe)  # 492


def test_recv_into_hashed_cannot_replay_offline(tmp_path):
    """A large recv_into recorded in hashed mode can't be replayed offline (494)."""
    def work():
        a, b = socket.socketpair()
        try:
            b.sendall(b"hello")
            buf = bytearray(64)
            n = a.recv_into(buf)
            return n, bytes(buf[:n])
        finally:
            a.close()
            b.close()

    out = tmp_path / "recvinto_h.flight"
    with flight.deterministic(str(out), io_hash_above=4):  # 5 bytes > 4 -> hashed
        assert work() == (5, b"hello")

    def probe():
        a, b = socket.socketpair()
        try:
            return a.recv_into(bytearray(64))
        finally:
            a.close()
            b.close()

    with pytest.raises(ReplayDivergence):
        flight.replay(str(out), probe)  # 494


def test_io_recorder_and_replayer_uninstall_swallow_setattr_error():
    """uninstall() must never raise even if restoring an attribute fails
    (IORecorder 424-425, IOReplayer 524-525). `int` is an immutable type, so
    setting an attribute on it raises TypeError inside the guarded loop."""
    from flight._io import IORecorder, IOReplayer

    rec = IORecorder(None, 0)
    rec._saved = [(int, "no_such_flight_attr", None)]
    rec.uninstall()  # 424-425 (except Exception: pass)
    assert rec._saved == []

    rep = IOReplayer(None)
    rep._saved = [(int, "no_such_flight_attr", None)]
    rep.uninstall()  # 524-525
    assert rep._saved == []


# -- subprocess result codec ------------------------------------------------


def test_subprocess_run_without_capture_encodes_and_decodes_none_streams(tmp_path):
    """run() without capture_output -> stdout/stderr are None: _enc_stream None
    (534) on record, _dec_stream None (542) on replay."""
    def work():
        cp = subprocess.run(["true"])
        return cp.returncode, cp.stdout, cp.stderr

    out = tmp_path / "run_none.flight"
    with flight.deterministic(str(out), io_hash_above=0):
        original = work()
    assert original == (0, None, None)
    assert flight.replay(str(out), work) == original  # 542


def test_subprocess_run_bytes_streams_encode_and_decode(tmp_path):
    """capture_output without text -> bytes streams: _enc_stream bytes (537)."""
    def work():
        cp = subprocess.run(["echo", "hi"], capture_output=True)
        return cp.returncode, cp.stdout

    out = tmp_path / "run_bytes.flight"
    with flight.deterministic(str(out), io_hash_above=0):
        original = work()
    assert original[1].strip() == b"hi"
    assert flight.replay(str(out), work) == original  # 537 (enc) / bytes dec


def test_subprocess_check_output_decoded_on_replay(tmp_path):
    """check_output replayed by actually calling it -> _decode_proc check_output
    branch (562)."""
    def work():
        return subprocess.check_output(["echo", "captured"])

    out = tmp_path / "co.flight"
    with flight.deterministic(str(out), io_hash_above=0):
        original = work()
    assert original.strip() == b"captured"
    assert flight.replay(str(out), work) == original  # 562


# ===========================================================================
# _record.py
# ===========================================================================


def _returns_locals():
    """Helper called *inside* a record scope: its frame's final writes are only
    seen by capture_return (no trailing LINE event)."""
    inner_a = 10
    inner_b = inner_a + 5
    return inner_b


def test_capture_return_diffs_final_frame_writes(tmp_path):
    """A function returning inside the scope triggers capture_return (141, 144-148)."""
    out = tmp_path / "ret.flight"
    with flight.record(path=out):
        value = _returns_locals()  # noqa: F841
    rec = flight.read(out).recording()
    assert rec.mutations
    names = {m.name for m in rec.mutations}
    assert "inner_b" in names  # captured at return, not lost


def test_scope_internal_guards_directly():
    """Directly exercise _Scope internals. The scope-capture code normally runs
    inside a `sys.monitoring` LINE/RETURN callback whose execution coverage.py's
    own monitoring cannot observe, so these paths are driven with direct calls:
    the _Watch.diff emit branches (78/81/85/91), capture_return (141-148),
    _diff_frame's f_locals failure (155-156) and _emit_value's truncation
    guard (181)."""
    from flight._record import _Scope, _Watch
    from flight._scrub import Scrubber

    flight.install()
    try:
        session = flight._install._active
        code = sys._getframe().f_code

        # -- _Watch.diff emit branches (direct) ----------------------------
        scope = _Scope(session, None, Scrubber(()))

        d = {"a": 1}
        wd = _Watch(d, "d")
        d["a"] = 2          # existing-key change
        d["b"] = 3          # new key
        wd.diff(scope, code, 1, 0)   # dict item writes -> 78
        del d["b"]
        wd.diff(scope, code, 2, 0)   # dict deletion -> 81

        lst = [1]
        wl = _Watch(lst, "lst")
        lst[0] = 9
        lst.append(7)
        wl.diff(scope, code, 3, 0)   # list item writes -> 85

        class Obj:
            pass

        o = Obj()
        wo = _Watch(o, "o")
        o.x = 5
        wo.diff(scope, code, 4, 0)   # attr write -> 91
        assert scope.mutations

        # -- capture_return on the owning thread (diff + bookkeeping drop) --
        def helper():
            captured_local = 99  # noqa: F841
            return sys._getframe()

        ret_scope = _Scope(session, None, Scrubber(()))
        fr = helper()
        ret_scope.capture_return(fr.f_code, fr)  # owner matches -> 141, 143-148
        # mutations are raw tuples: (seq, kind, name, key, rendered, ...)
        assert any(m[2] == "captured_local" for m in ret_scope.mutations)

        # -- capture_return on a scope owned by another thread -> early exit
        foreign = _Scope(session, None, Scrubber(()))
        foreign.owner = -1  # not this thread
        here = sys._getframe()
        foreign.capture_return(here.f_code, here)  # 141 -> 142
        assert foreign.mutations == []

        # -- _diff_frame where frame.f_locals raises -> cur = None, no crash
        bad = _Scope(session, None, Scrubber(()))  # owner == this thread
        bad._diff_frame(here.f_code, 1, object(), 0)  # 155-156
        assert bad.mutations == []

        # -- _emit_value truncation guard: cap at 2, diff a frame with 5 locals
        capped = _Scope(session, None, Scrubber(()))
        capped.max_mutations = 2

        def many_locals():
            a = 1
            b = 2
            c = 3
            d2 = 4
            e = 5
            return sys._getframe()

        target = many_locals()
        capped._diff_frame(target.f_code, 1, target, id(target))  # 181 fires
        assert capped.truncated
        assert len(capped.mutations) == 2
    finally:
        flight.uninstall()


def test_watch_variants_dict_list_attr_and_deletion(tmp_path):
    """watch() over a dict (item change 78 + deletion 81), a list (85) and an
    object (attr 91)."""
    class Obj:
        pass

    out = tmp_path / "watch.flight"
    with flight.record(path=out):
        d = {"a": 1}
        lst = [1]
        o = Obj()
        flight.watch(d, name="d")
        flight.watch(lst, name="lst")
        flight.watch(o, name="o")
        d["a"] = 2       # existing-key change -> 78
        d["b"] = 3       # new key -> 78
        lst[0] = 9       # list change -> 85
        lst.append(7)    # list grow -> 85
        o.x = 5          # attr write -> 91
        del d["b"]       # deletion -> 81
        marker = 0       # noqa: F841  (a trailing LINE so the deletion is diffed)

    rec = flight.read(out).recording()
    assert any(m.name == "d" for m in rec.mutations)
    assert any(m.name == "lst" for m in rec.mutations)
    assert any(m.name == "o" for m in rec.mutations)


def test_watch_snapshot_and_diff_swallow_errors():
    """_Watch._snapshot (69-70) and _Watch.diff (93-94) must never raise even if
    the watched object's items() blows up."""
    from flight._record import _Watch

    class BadDict(dict):
        def items(self):
            raise RuntimeError("boom")

    bd = BadDict()
    w = _Watch(bd, "bd")  # snapshot's items() raises -> 69-70
    assert w.snap == {}
    # diff's items() raises too -> 93-94 (scope is never touched)
    w.diff(object(), None, 1, 0)


def test_record_watch_param_and_default_label(tmp_path):
    """record(watch=[...]) applies watches in __enter__ (258) with a default
    label (330-331)."""
    d = {}
    out = tmp_path / "wp.flight"
    with flight.record(path=out, watch=[d]):
        d["k"] = 1
        marker = 0  # noqa: F841
    rec = flight.read(out).recording()
    assert rec.mutations


def test_record_default_path_when_none(tmp_path, monkeypatch):
    """No explicit path -> _Scope.write computes a default scope path (208)."""
    monkeypatch.chdir(tmp_path)
    with flight.record():  # path is None
        x = 1  # noqa: F841
    # a scope .flight was written under cwd
    assert any(p.suffix == ".flight" for p in tmp_path.iterdir())


def test_record_write_failure_returns_none(tmp_path):
    """A path whose directory does not exist makes dump_scope fail; write()
    swallows it and returns None (233-234) without breaking the program."""
    bad = tmp_path / "no_such_subdir" / "x.flight"
    with flight.record(path=bad):  # must not raise
        y = 1  # noqa: F841
    assert not bad.exists()


def test_record_exit_swallows_capture_and_exit_scope_errors(tmp_path):
    """__exit__ guards the final capture_line (272-273) and _exit_scope
    (276-277); both must be swallowed."""
    rec = flight.record(path=tmp_path / "guard.flight")
    scope = rec.__enter__()
    try:
        def _boom(*_a, **_k):
            raise RuntimeError("boom")

        scope.capture_line = _boom            # 272-273
        scope.session._exit_scope = _boom     # 276-277
    finally:
        # Must not raise despite both callbacks blowing up.
        assert rec.__exit__(None, None, None) is False


def test_read_source_swallows_errors(monkeypatch):
    """_read_source returns None if linecache raises (341-342)."""
    from flight import _record

    def _boom(_fn):
        raise RuntimeError("boom")

    monkeypatch.setattr("linecache.getlines", _boom)
    assert _record._read_source("something.py") is None


def test_record_cwd_swallows_errors(monkeypatch):
    """_record._cwd returns '' if os.getcwd raises (349-350)."""
    from flight import _record

    def _boom():
        raise OSError("no cwd")

    monkeypatch.setattr(os, "getcwd", _boom)
    assert _record._cwd() == ""


# ===========================================================================
# _nondet.py
# ===========================================================================


def test_interposer_install_skips_missing_sources(monkeypatch):
    """_Interposer.install continues past a missing module / attribute (259-260)."""
    from flight import _nondet

    monkeypatch.setattr(
        _nondet,
        "_SOURCES",
        (("no_such_module_xyzzy", "foo"), ("os", "no_such_attr_xyzzy")),
    )
    ip = _nondet._Interposer(lambda source, orig: orig)
    ip.install()  # both entries hit the except -> 259-260
    assert ip._saved == []
    ip.uninstall()


def test_interposer_uninstall_swallows_setattr_error():
    """_Interposer.uninstall swallows a failing setattr (269-270)."""
    from flight import _nondet

    ip = _nondet._Interposer(lambda s, o: o)
    ip._saved = [(int, "no_such_flight_attr", None)]
    ip.uninstall()  # 269-270
    assert ip._saved == []


def test_recorder_record_value_falls_back_on_encode_error():
    """record_value uses the repr fallback when _encode raises (319-320)."""
    from flight import _nondet

    rec = _nondet._Recorder()
    rec.record_value("some.source", {"k": object()})  # dict -> json.dumps raises
    assert rec.entries, "an entry was still recorded"
    _seq, _src, tag, _payload = rec.entries[-1]
    assert tag == "r"  # best-effort repr fallback


def test_deterministic_tape_property(tmp_path):
    """The .tape property builds a Tape from the recorder entries (409)."""
    with flight.deterministic(str(tmp_path / "t.flight"), io_hash_above=0) as det:
        _ = round(__import__("time").time(), 6)
        tape = det.tape
    assert isinstance(tape, Tape)


def test_deterministic_write_failure_is_swallowed(tmp_path):
    """A bad output path makes dump_nondet fail; _write swallows it (428-429)."""
    bad = tmp_path / "no_such_subdir" / "run.flight"
    with flight.deterministic(str(bad), io_hash_above=0):
        _ = round(__import__("time").time(), 6)
    assert not bad.exists()


def test_deterministic_crash_write_falls_back(tmp_path):
    """A crash inside a scope whose output dir does not exist: _write_crash fails
    and falls back to _write (which also fails) — the guard swallows both
    (454-457), and the user's exception still propagates."""
    bad = tmp_path / "no_such_subdir" / "crash.flight"
    with pytest.raises(ValueError):
        with flight.deterministic(str(bad), io_hash_above=0):
            raise ValueError("boom")
    assert not bad.exists()


def test_default_path_and_cwd_helpers(monkeypatch):
    """_default_path (536-538) and _cwd's error guard (544-545)."""
    from flight import _nondet

    p = _nondet._default_path()
    assert p.endswith(".flight") and "flight-run-" in p

    def _boom():
        raise OSError("no cwd")

    monkeypatch.setattr(os, "getcwd", _boom)
    assert _nondet._cwd() == ""


# ===========================================================================
# _threads.py
# ===========================================================================


def test_thread_base_uninstall_swallows_setattr_error():
    """_ThreadBase.uninstall swallows a failing setattr in its saved loop
    (113-114)."""
    from flight._threads import ThreadRecorder

    tr = ThreadRecorder(None)
    tr._orig_start = None  # skip Thread.start restore
    tr._saved = [(int, "no_such_flight_attr", None)]
    tr.uninstall()  # 113-114
    assert tr._saved == []


def test_rec_lock_proxy_methods():
    """_RecLock.release (139), .locked (142) and __getattr__ delegation (145)."""
    from flight._threads import _RecLock

    class _Tracer:
        def __init__(self):
            self.n = 0

        def on_acquire(self):
            self.n += 1

    tracer = _Tracer()
    rl = _RecLock(threading.Lock(), tracer)
    assert rl.acquire() is True      # gated -> on_acquire
    assert tracer.n == 1
    rl.release()                     # 139
    assert rl.locked() is False      # 142
    with pytest.raises(AttributeError):
        rl.no_such_attribute         # __getattr__ delegates to the real lock (145)


def test_replay_lock_proxy_methods():
    """_ReplayLock non-gated acquire (187), release (194), locked (197),
    __getattr__ delegation (200)."""
    from flight._threads import _ReplayLock, ThreadReplayer

    tracer = ThreadReplayer([])
    rl = _ReplayLock(threading.Lock(), tracer)
    assert rl.acquire(blocking=False) is True  # not gated -> 187
    rl.release()                                # 194
    assert rl.locked() is False                 # 197
    with pytest.raises(AttributeError):
        rl.no_such_attribute                    # 200


# ===========================================================================
# _asyncio.py
# ===========================================================================


def test_task_factory_typeerror_fallback():
    """_TaskOrder._factory falls back to the minimal Task() signature when the
    kwargs form raises TypeError (52-54)."""
    import asyncio

    from flight._asyncio import _TaskOrder

    async def coro():
        return 42

    loop = asyncio.new_event_loop()
    try:
        order = _TaskOrder()
        task = order._factory(loop, coro(), definitely_not_a_task_kwarg=1)  # 52-54
        assert loop.run_until_complete(task) == 42
    finally:
        loop.close()


def test_task_order_install_skips_missing_attr(monkeypatch):
    """install() continues past a module missing new_event_loop (65-66)."""
    import asyncio
    import asyncio.events as events

    from flight._asyncio import _TaskOrder

    monkeypatch.delattr(asyncio, "new_event_loop", raising=False)
    monkeypatch.delattr(events, "new_event_loop", raising=False)
    order = _TaskOrder()
    order.install()  # both getattr raise AttributeError -> 65-66
    assert order._saved == []
    order.uninstall()


def test_wrap_new_loop_swallows_set_task_factory_error():
    """_wrap_new_loop swallows a set_task_factory failure (75-76)."""
    from flight._asyncio import _TaskOrder

    class FakeLoop:
        def set_task_factory(self, _f):
            raise RuntimeError("nope")

    order = _TaskOrder()
    wrapped = order._wrap_new_loop(lambda *a, **k: FakeLoop())
    loop = wrapped()  # set_task_factory raises -> 75-76
    assert isinstance(loop, FakeLoop)


def test_task_order_uninstall_swallows_setattr_error():
    """_TaskOrder.uninstall swallows a failing setattr (85-86)."""
    from flight._asyncio import _TaskOrder

    order = _TaskOrder()
    order._saved = [(int, "no_such_flight_attr", None)]
    order.uninstall()  # 85-86
    assert order._saved == []


def test_asyncio_replayer_finalize_detects_divergence():
    """AsyncioReplayer.finalize raises when the observed completion order does
    not match the recorded order (117)."""
    from flight._asyncio import AsyncioReplayer

    tape = _tape(("asyncio.order", "s", json.dumps([0, 1])))
    replayer = AsyncioReplayer(tape)  # pops the control entry, expects [0, 1]
    # No tasks ran, so completed stays [] != [0, 1].
    with pytest.raises(ReplayDivergence):
        replayer.finalize()  # 117
