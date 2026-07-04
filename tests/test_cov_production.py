"""Coverage top-up for the Phase-8/10 + config modules.

Every test here targets a specific line that the main suite doesn't reach,
almost always a defensive/error branch (P1: "a failure in the machinery never
escapes"). The what-if tracer body is exercised by *calling the trace function
directly* rather than through ``sys.settrace`` — under ``settrace`` the coverage
tracer is displaced, so those lines run in the real suite but are invisible to
the measurer; a direct call both exercises and measures them.
"""

from __future__ import annotations

import atexit
import os
import sys
from pathlib import Path

import pytest

import flight
from flight._config import Config, _stdlib_and_site_prefixes

VALID_TP = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"


# =========================================================================
# _whatif
# =========================================================================


def test_safe_repr_survives_a_hostile_repr():
    from flight._whatif import _safe_repr

    class Hostile:
        def __repr__(self):  # noqa: D401
            raise ValueError("no repr for you")

    out = _safe_repr(Hostile())
    assert out.startswith("<repr failed: ValueError")


def test_render_note_when_pep667_absent(monkeypatch):
    from flight import _whatif
    from flight._whatif import Outcome, Override, WhatIf

    monkeypatch.setattr(_whatif, "_PEP667", False)
    wi = WhatIf(
        baseline=Outcome(returned=1),
        counterfactual=Outcome(returned=2),
        overrides=[Override("x", 1, line=1)],
    )
    text = wi.render()
    assert "needs Python 3.13" in text


@pytest.mark.skipif(
    sys.version_info < (3, 13), reason="write-through locals need 3.13"
)
def test_tracer_body_applies_skips_and_matches_directly():
    """Drive ``_make_tracer``'s trace function directly (not via settrace, which
    would displace the coverage tracer). One override matches and fires; one is
    skipped on qualname; one is skipped on line."""
    from flight._whatif import Override, _make_tracer

    def drive():
        x = "orig"
        # All on one physical line so f_lineno is constant across the calls and
        # equals the matching override's target line.
        frame = sys._getframe(); ln = frame.f_lineno; hit = Override("x", "new", line=ln, qualname=None); wrong_q = Override("x", 1, line=ln, qualname="does.not.match"); wrong_l = Override("x", 1, line=10 ** 9, qualname=None); t = _make_tracer([wrong_q, wrong_l, hit]); assert t(frame, "call", None) is t; t(frame, "line", None)
        return x, hit

    value, hit = drive()
    assert value == "new"  # PEP 667 write-through updated the live local
    assert hit.applied and hit.previous == "'orig'"


def test_tracer_apply_failure_is_swallowed():
    """If reading/writing the frame's locals blows up, the override is silently
    left un-applied (the inner ``except Exception: pass``)."""
    from flight._whatif import Override, _make_tracer

    class BadFrame:
        f_code = type("C", (), {"co_qualname": "x"})
        f_lineno = 5

        @property
        def f_locals(self):
            raise RuntimeError("no locals here")

    ov = Override("z", 1, line=5, qualname=None)
    t = _make_tracer([ov])
    t(BadFrame(), "line", None)  # must not raise
    assert not ov.applied


# =========================================================================
# _config
# =========================================================================


def test_stdlib_prefixes_swallow_probe_failures(monkeypatch):
    """Each of the three environment probes is wrapped in a best-effort guard;
    make all three raise and confirm we still return (at least Flight's dir)."""
    import site
    import sysconfig

    def boom(*a, **k):
        raise RuntimeError("probe unavailable")

    monkeypatch.setattr(sysconfig, "get_paths", boom)
    monkeypatch.setattr(site, "getsitepackages", boom)
    monkeypatch.setattr(site, "getusersitepackages", boom)

    prefixes = _stdlib_and_site_prefixes()
    # Flight's own package dir is added unconditionally after the guards.
    flight_dir = os.path.realpath(str(Path(flight.__file__).resolve().parent))
    assert flight_dir in prefixes


# =========================================================================
# _correlation
# =========================================================================


def test_from_otel_returns_none_without_the_sdk():
    """opentelemetry is not installed here, so the import inside ``from_otel``
    fails and the method degrades to ``None`` (never raises)."""
    from flight._correlation import TraceContext

    assert TraceContext.from_otel(service="svc") is None


def test_resolve_uses_the_otel_branch(monkeypatch):
    """When ``from_otel`` yields a context it wins over the environment."""
    from flight._correlation import TraceContext, resolve

    def fake_from_otel(service=None):
        return TraceContext.parse(VALID_TP, service=service)

    monkeypatch.setattr(TraceContext, "from_otel", staticmethod(fake_from_otel))
    ctx = resolve(from_otel=True, from_env=False, service="svc")
    assert ctx is not None
    assert ctx.trace_id == "4bf92f3577b34da6a3ce929d0e0e4736"
    assert ctx.service == "svc"


# =========================================================================
# _crypto
# =========================================================================


def test_crypto_unavailable_message():
    from flight._crypto import CryptoUnavailable

    assert "cryptography" in str(CryptoUnavailable())


def test_aesgcm_and_is_available_when_import_fails(monkeypatch):
    """Simulate a missing/broken ``cryptography`` by poisoning the import; the
    seal step raises :class:`CryptoUnavailable` and ``is_available`` is False."""
    from flight import _crypto

    monkeypatch.setitem(
        sys.modules, "cryptography.hazmat.primitives.ciphers.aead", None
    )
    with pytest.raises(_crypto.CryptoUnavailable):
        _crypto._aesgcm()
    assert _crypto.is_available() is False


def test_crypto_round_trip_and_tamper(tmp_path):
    from flight import _crypto

    secret = b"black-box bytes with real values"
    blob = _crypto.encrypt_bytes(secret, "hunter2")
    assert _crypto.decrypt_bytes(blob, "hunter2") == secret
    # wrong passphrase and tampering both fail AEAD authentication.
    with pytest.raises(_crypto.DecryptError):
        _crypto.decrypt_bytes(blob, "wrong")
    tampered = bytearray(blob)
    tampered[-1] ^= 0x01
    with pytest.raises(_crypto.DecryptError):
        _crypto.decrypt_bytes(bytes(tampered), "hunter2")


def test_decrypt_file_appends_flight_when_not_dot_enc(tmp_path):
    """The default output for an input that does *not* end in ``.enc`` appends
    ``.flight`` rather than stripping a suffix."""
    from flight import _crypto

    src = tmp_path / "payload.bin"  # suffix is .bin, not .enc
    src.write_bytes(_crypto.encrypt_bytes(b"hello world", "pw"))
    out = _crypto.decrypt_file(src, "pw")  # out_path omitted → else branch
    assert out == src.with_suffix(".bin.flight")
    assert out.read_bytes() == b"hello world"


# =========================================================================
# _governor
# =========================================================================


def test_read_events_uses_default_core_source():
    from flight._governor import LEVEL_LINES, Governor

    gov = Governor(baseline=LEVEL_LINES)  # stats_source=None → default _core
    n = gov._read_events()
    assert isinstance(n, int) and n >= 0
    assert gov._stats_source is not None  # memoised the built default


def test_on_change_callback_failure_is_swallowed():
    from flight._governor import LEVEL_LINES, LEVEL_RETURNS, Governor

    t = {"v": 0.0}
    ev = {"v": 0}
    gov = Governor(
        baseline=LEVEL_LINES,
        ceiling=0.03,
        per_event_ns=65.0,
        stats_source=lambda: {"total_events": ev["v"]},
        apply=lambda level: None,
        clock=lambda: t["v"],
        on_change=lambda *a: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    gov.tick()  # baseline
    for _ in range(3):
        t["v"] += 1.0
        ev["v"] += 1_000_000
        gov.tick()  # demotes → on_change raises → swallowed
    assert gov.ladder.level == LEVEL_RETURNS  # the demotion still happened


def test_do_apply_default_path_and_error_swallowed():
    from flight._governor import LEVEL_CALLS, LEVEL_LINES, Governor

    # apply=None → default import of _set_ring_level (no active session: no-op).
    gov = Governor(baseline=LEVEL_LINES, apply=None)
    gov._do_apply(LEVEL_CALLS)  # must not raise

    # a failing apply callback is swallowed too.
    boom = Governor(
        baseline=LEVEL_LINES,
        apply=lambda level: (_ for _ in ()).throw(RuntimeError("nope")),
    )
    boom._do_apply(LEVEL_CALLS)  # must not raise


def test_run_loop_swallows_tick_errors():
    from flight._governor import LEVEL_LINES, Governor

    gov = Governor(baseline=LEVEL_LINES)

    class FakeStop:
        def __init__(self):
            self.n = 0

        def wait(self, _timeout):
            self.n += 1
            return self.n > 1  # run the body exactly once, then stop

    gov._stop = FakeStop()

    def boom():
        raise RuntimeError("tick blew up")

    gov.tick = boom  # type: ignore[method-assign]
    gov._run()  # body runs once, tick raises, loop swallows and exits


# =========================================================================
# _daemon
# =========================================================================


def _config(tmp_path) -> Config:
    return Config(output_dir=tmp_path)


def test_promote_swallows_replace_failure(tmp_path, monkeypatch):
    from flight import _daemon

    ckpt = tmp_path / "ckpt.flight"
    ckpt.write_bytes(b"data")
    final = tmp_path / "final.flight"

    def boom(*a, **k):
        raise OSError("replace failed")

    monkeypatch.setattr(_daemon.os, "replace", boom)
    assert _daemon._promote(ckpt, final) is None


def test_supervise_breaks_on_read_error_and_closes(tmp_path):
    """A bad read fd makes ``os.read`` raise (break) and the finally-close also
    raise (swallowed). No clean byte → an unclean death, checkpoint absent →
    nothing to promote."""
    from flight._daemon import supervise

    r, w = os.pipe()
    os.close(r)
    os.close(w)
    result = supervise(r, tmp_path / "absent.flight", tmp_path, os.getpid())
    assert result is None  # no checkpoint existed to promote


def test_supervise_clean_shutdown_unlink_failure_is_swallowed(tmp_path):
    """Clean shutdown where discarding the checkpoint fails (it is a directory,
    so ``unlink`` raises) still returns None without escaping."""
    from flight._daemon import _CLEAN, supervise

    ckpt_dir = tmp_path / "ckpt.flight"
    ckpt_dir.mkdir()  # unlink() on a directory raises → the guard swallows it
    r, w = os.pipe()
    os.write(w, _CLEAN)
    os.close(w)
    assert supervise(r, ckpt_dir, tmp_path, os.getpid()) is None


def test_start_is_idempotent(tmp_path):
    from flight._daemon import Daemon

    d = Daemon(_config(tmp_path))
    d._started = True  # pretend already running
    assert d.start() is d  # early-return branch, no supervisor spawned
    assert d._proc is None


def test_atexit_swallows_stop_failure(tmp_path):
    from flight._daemon import Daemon

    d = Daemon(_config(tmp_path))

    def boom(**k):
        raise RuntimeError("stop failed")

    d.stop = boom  # type: ignore[method-assign]
    d._atexit()  # must not raise


def test_write_checkpoint_cleans_up_temp_on_failure(tmp_path, monkeypatch):
    from flight import _daemon

    ckpt = tmp_path / "ck.flight"
    d = _daemon.Daemon(_config(tmp_path), checkpoint=ckpt)
    # The temp sibling the writer targets; make it a directory so the cleanup
    # unlink() *also* fails, exercising the nested guard.
    tmp = ckpt.with_suffix(ckpt.suffix + ".tmp")
    tmp.mkdir()

    def boom(*a, **k):
        raise RuntimeError("dump failed")

    monkeypatch.setattr("flight._install._write_ring_dump", boom)
    d._write_checkpoint()  # must not raise; both guards fire
    assert tmp.is_dir()  # untouched (unlink failed and was swallowed)


def test_stop_swallows_every_teardown_failure(tmp_path, monkeypatch):
    """A fully hostile teardown: a closed write fd (write + close raise), a proc
    whose wait raises, and an atexit.unregister that raises — all swallowed."""
    from flight._daemon import Daemon

    d = Daemon(_config(tmp_path))
    d._started = True
    d._thread = None

    r, w = os.pipe()
    os.close(r)
    os.close(w)
    d._write_fd = w  # closed → os.write and os.close both raise

    class FakeProc:
        def wait(self, timeout=None):
            raise RuntimeError("wait failed")

    d._proc = FakeProc()

    def boom(_fn):
        raise RuntimeError("unregister failed")

    monkeypatch.setattr(atexit, "unregister", boom)

    d.stop(clean=True)  # must not raise
    assert d._started is False
    assert d._write_fd is None
    assert d._proc is None


def test_stop_before_start_is_a_noop(tmp_path):
    from flight._daemon import Daemon

    d = Daemon(_config(tmp_path))
    d.stop()  # not started → returns immediately
    assert d._started is False
