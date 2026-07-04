"""Coverage-completion tests for ``flight._install`` and ``flight._cli``.

Each test drives a specific uncovered line/branch directly (exception hooks,
lifecycle idempotency, error swallowing, CLI subcommand handlers and their
error paths). Nothing here changes behaviour — it only exercises it.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import threading
import types

import pytest

import flight
from flight import _cli, _install


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _boom(*_a, **_k):
    raise RuntimeError("injected fault")


def _make_crash(tmp_path, name="c.flight", *, correlate=False, link=False):
    """Write a real crash `.flight`, optionally with a trace context + link."""
    flight.install(output_dir=tmp_path)
    if correlate:
        flight.correlate(root=True)
    if link:
        flight.link(str(tmp_path / "up.flight"), service="up")
    p = tmp_path / name
    try:
        raise ValueError("boom crash")
    except ValueError:
        flight.capture(path=p)
    flight.uninstall()
    assert p.exists()
    return p


def _make_scope(tmp_path, name="s.flight"):
    """Write a real scope `.flight` that contains a container (item) mutation."""
    p = tmp_path / name
    with flight.record(path=str(p)) as rec:
        data = {}
        rec.watch(data, "data")
        data["k"] = 1
        x = 1
        x = 2  # noqa: F841
    if flight.is_installed():
        flight.uninstall()
    assert p.exists()
    return p


class _FakeScope:
    def __init__(self):
        self.lines = []
        self.returns = 0

    def capture_line(self, code, line, frame):
        self.lines.append(line)

    def capture_return(self, code, frame):
        self.returns += 1


class _RaisingScope:
    def capture_line(self, *a):
        raise RuntimeError("scope fault")

    def capture_return(self, *a):
        raise RuntimeError("scope fault")


# --------------------------------------------------------------------------
# _install.py — ring granularity
# --------------------------------------------------------------------------

def test_set_ring_level_swallows_set_events_error(tmp_path, monkeypatch):
    # 95-96: except Exception: pass around _mon.set_events.
    flight.install(output_dir=tmp_path)
    monkeypatch.setattr(_install._mon, "set_events", _boom)
    _install._active.set_ring_level(0)  # must not raise
    monkeypatch.undo()
    flight.uninstall()


def test_wanted_computes_then_caches(tmp_path):
    # 99-103: first call computes + stores, second hits the cache.
    flight.install(output_dir=tmp_path)
    s = _install._active
    fname = (lambda: None).__code__.co_filename  # this test file -> interesting
    first = s._wanted(fname)
    assert fname in s._interesting
    assert s._wanted(fname) == first  # cached branch
    flight.uninstall()


# --------------------------------------------------------------------------
# _install.py — scope capture callbacks (tool 3), driven directly
# --------------------------------------------------------------------------

def test_scope_callbacks_direct(tmp_path):
    # 108-137: line/return/unwind callbacks, all branches incl. except paths.
    flight.install(output_dir=tmp_path)
    s = _install._active
    tid = threading.get_ident()
    interesting = (lambda: None).__code__          # this file -> wanted
    not_wanted = os.path.join.__code__             # stdlib -> not wanted

    # on_line: not wanted -> DISABLE (108-110)
    assert s._scope_on_line(not_wanted, 1) is _install._mon.DISABLE

    # on_line: wanted + active scope -> capture_line (111-113, 116)
    fake = _FakeScope()
    s._scopes = {tid: [fake]}
    assert s._scope_on_line(interesting, 7) is None
    assert fake.lines == [7]

    # on_line: capture raises -> except (114-115)
    s._scopes = {tid: [_RaisingScope()]}
    assert s._scope_on_line(interesting, 9) is None

    # on_return: wanted + scope -> capture_return (119-124, 127)
    fake = _FakeScope()
    s._scopes = {tid: [fake]}
    assert s._scope_on_return(interesting, 0, None) is None
    assert fake.returns == 1
    # on_return: not wanted (skip) still returns None
    assert s._scope_on_return(not_wanted, 0, None) is None
    # on_return: capture raises -> except (125-126)
    s._scopes = {tid: [_RaisingScope()]}
    assert s._scope_on_return(interesting, 0, None) is None

    # on_unwind: wanted + scope (130-134, 137)
    fake = _FakeScope()
    s._scopes = {tid: [fake]}
    assert s._scope_on_unwind(interesting, 0, None) is None
    assert fake.returns == 1
    # on_unwind: capture raises -> except (135-136)
    s._scopes = {tid: [_RaisingScope()]}
    assert s._scope_on_unwind(interesting, 0, None) is None

    s._scopes = {}
    flight.uninstall()


# --------------------------------------------------------------------------
# _install.py — exception hooks
# --------------------------------------------------------------------------

def test_exception_hooks(tmp_path, monkeypatch):
    # Neutralize the interpreter hooks so the session captures no-op "prev"
    # hooks (and so uninstall restores those, not real ones).
    monkeypatch.setattr(sys, "excepthook", lambda *a: None)
    monkeypatch.setattr(threading, "excepthook", lambda a: None)
    monkeypatch.setattr(sys, "unraisablehook", lambda u: None)

    flight.install(output_dir=tmp_path)
    s = _install._active
    try:
        raise KeyError("k")
    except KeyError:
        et, ev, tb = sys.exc_info()

    import flight._capture as cap
    orig = cap.write_crash_flight

    # _excepthook success path (148-150): a crash file is written + announced.
    s._excepthook(et, ev, tb)

    # _excepthook except path (151-152): make the capture raise.
    monkeypatch.setattr(cap, "write_crash_flight", _boom)
    s._excepthook(et, ev, tb)

    # _threading_excepthook normal (156-160, 163-164) then except (161-162).
    monkeypatch.setattr(cap, "write_crash_flight", orig)
    args = types.SimpleNamespace(exc_type=KeyError, exc_value=ev, exc_traceback=tb, thread=None)
    s._threading_excepthook(args)
    monkeypatch.setattr(cap, "write_crash_flight", _boom)
    s._threading_excepthook(args)

    # _unraisable_hook except path (180-181).
    un = types.SimpleNamespace(exc_type=KeyError, exc_value=ev, exc_traceback=tb)
    s._unraisable_hook(un)

    monkeypatch.setattr(cap, "write_crash_flight", orig)
    flight.uninstall()


# --------------------------------------------------------------------------
# _install.py — Phase-8 lifecycle (install branches + idempotency)
# --------------------------------------------------------------------------

def test_install_daemon_and_start_twice(tmp_path):
    # 222-223 (install starts daemon) and 247-248 (start_daemon returns existing).
    flight.install(output_dir=tmp_path, daemon=True)
    s = _install._active
    assert s._daemon is not None
    assert s.start_daemon() is s._daemon
    flight.uninstall()


def test_install_governor_and_start_twice(tmp_path):
    # 220-221 (install starts governor) and 230-231 (start_governor returns existing).
    flight.install(output_dir=tmp_path, overhead_slo=0.05)
    s = _install._active
    assert s._governor is not None
    assert s.start_governor() is s._governor
    flight.uninstall()


# --------------------------------------------------------------------------
# _install.py — scope tool teardown + uninstall paths
# --------------------------------------------------------------------------

def test_disable_scope_tool_swallows_error(tmp_path, monkeypatch):
    # 287-288: except around set_events/register_callback/free_tool_id.
    flight.install(output_dir=tmp_path)
    s = _install._active
    s._enable_scope_tool()
    monkeypatch.setattr(_install._mon, "set_events", _boom)
    s._disable_scope_tool()  # must not raise
    monkeypatch.undo()
    flight.uninstall()
    # The injected fault skipped free_tool_id(TOOL_SCOPE); release it so it
    # doesn't leak into later tests.
    try:
        _install._mon.free_tool_id(_install.TOOL_SCOPE)
    except Exception:
        pass


def test_uninstall_returns_early_when_not_installed():
    # 298-299: uninstall on a never-installed session returns immediately.
    s = _install._Session(flight.Config())
    s.uninstall()
    assert s._installed is False


def test_uninstall_governor_and_daemon_stop_raise(tmp_path):
    # 303-304 and 307-311: stop() raising is swallowed; refs cleared.
    flight.install(output_dir=tmp_path)
    s = _install._active

    class _Boom:
        def stop(self, *a, **k):
            raise RuntimeError("stop fault")

    s._governor = _Boom()
    s._daemon = _Boom()
    flight.uninstall()
    assert s._governor is None and s._daemon is None


def test_uninstall_swallows_mon_error(tmp_path, monkeypatch):
    # 325-326: except around the ring-tool teardown.
    flight.install(output_dir=tmp_path)
    monkeypatch.setattr(_install._mon, "set_events", _boom)
    flight.uninstall()  # must not raise
    monkeypatch.undo()
    assert not flight.is_installed()
    # The injected fault skipped free_tool_id(TOOL_RING); release it so the
    # next install() doesn't hit "tool 2 already in use".
    try:
        _install._mon.free_tool_id(_install.TOOL_RING)
    except Exception:
        pass


# --------------------------------------------------------------------------
# _install.py — dump / correlation / module-level entry points
# --------------------------------------------------------------------------

def test_dump_default_path(tmp_path):
    # 378 (path is None -> crash_path) and 505-507 (_pid).
    flight.install(output_dir=tmp_path)
    p = flight.dump()
    assert p is not None and os.path.exists(p)
    flight.uninstall()


def test_dump_with_correlation(tmp_path):
    # 400 (dump_nondet branch) and 413-414 (_correlation_entries success).
    flight.install(output_dir=tmp_path)
    flight.correlate(root=True)
    p = flight.dump(path=tmp_path / "d.flight")
    assert p is not None and os.path.exists(p)
    flight.uninstall()


def test_correlation_entries_swallows_error():
    # 415-416: to_nondet raising -> [].
    cfg = flight.Config()
    cfg.correlation = types.SimpleNamespace(to_nondet=_boom)
    assert _install._correlation_entries(cfg) == []


def test_module_set_ring_level(tmp_path):
    # 421-422: _set_ring_level delegates to the active session.
    flight.install(output_dir=tmp_path)
    _install._set_ring_level(0)  # must not raise
    flight.uninstall()


def test_start_daemon_none_when_not_installed():
    # 430-431: returns None with no active session.
    assert flight.start_daemon() is None


def test_start_daemon_when_installed_applies_overrides(tmp_path):
    # 432-434: overrides applied, then the daemon is started.
    flight.install(output_dir=tmp_path)
    d = flight.start_daemon(daemon_interval=2.0)
    assert d is not None
    assert _install._active.config.daemon_interval == 2.0
    flight.uninstall()


def test_start_governor_none_when_not_installed():
    # 443-444: returns None with no active session.
    assert flight.start_governor() is None


def test_start_governor_defaults_slo_when_installed(tmp_path):
    # 445-449: overrides loop, default slo, and start.
    flight.install(output_dir=tmp_path)
    # Pass a (non-slo) override so the overrides loop body runs (446), while
    # overhead_slo stays None so it gets defaulted to 0.03 (447-448).
    g = flight.start_governor(governor_interval=0.5)
    assert g is not None
    assert _install._active.config.overhead_slo == 0.03
    flight.uninstall()


def test_link_none_when_not_installed():
    # 493-494: returns None with no active session.
    assert flight.link("x.flight") is None


def test_link_mints_root_when_no_context(tmp_path):
    # 495-497: no existing correlation -> new_root, then a link is added.
    flight.install(output_dir=tmp_path)
    ctx = flight.link(str(tmp_path / "u.flight"), service="svc")
    assert ctx is not None and len(ctx.links) == 1
    flight.uninstall()


def test_cwd_swallows_error(tmp_path, monkeypatch):
    # 515-516: os.getcwd() raising -> "".
    monkeypatch.setattr(os, "getcwd", _boom)
    assert _install._cwd() == ""


# --------------------------------------------------------------------------
# _cli.py
# --------------------------------------------------------------------------

def test_cmd_run_correlate_and_return_zero(tmp_path):
    # 27-29 (--correlate) and 37 (clean return 0). Optionals must precede the
    # script positional because script_args uses argparse.REMAINDER.
    script = tmp_path / "ok.py"
    script.write_text("x = 1\nprint('hi')\n")
    saved_argv, saved_path = list(sys.argv), list(sys.path)
    try:
        rc = _cli.main(["run", "--correlate", "--output-dir", str(tmp_path), str(script)])
    finally:
        sys.argv[:] = saved_argv
        sys.path[:] = saved_path
    assert rc == 0
    if flight.is_installed():
        flight.uninstall()


def test_cmd_run_systemexit(tmp_path):
    # 38-39: a script raising SystemExit(code) -> that code is returned.
    script = tmp_path / "bye.py"
    script.write_text("import sys\nsys.exit(3)\n")
    saved_argv, saved_path = list(sys.argv), list(sys.path)
    try:
        rc = _cli.main(["run", "--output-dir", str(tmp_path), str(script)])
    finally:
        sys.argv[:] = saved_argv
        sys.path[:] = saved_path
    assert rc == 3
    if flight.is_installed():
        flight.uninstall()


def test_fmt_time_unknown():
    # 46: falsy timestamp -> "unknown".
    assert _cli._fmt_time(0) == "unknown"


def test_cmd_timeline_container_target(tmp_path, capsys):
    # 151: container (item) mutation target rendered as name[key].
    p = _make_scope(tmp_path)
    rc = _cli.main(["timeline", str(p)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "data[k]" in out


def test_cmd_repro_notes_and_verified(monkeypatch):
    # 168-176: notes loop, approximate, verified True/False.
    fake = types.SimpleNamespace(
        script="code", path="repro_bug.py", notes=["n1", "n2"],
        approximate=True, verified=True,
    )
    monkeypatch.setattr("flight._repro.write_repro", lambda *a, **k: fake)
    ns = argparse.Namespace(file="x", output=None, no_verify=True, pytest=False)
    assert _cli._cmd_repro(ns) == 0

    fake2 = types.SimpleNamespace(
        script="code", path="repro_bug.py", notes=[], approximate=False, verified=False,
    )
    monkeypatch.setattr("flight._repro.write_repro", lambda *a, **k: fake2)
    assert _cli._cmd_repro(ns) == 0

    # 166-167: no script could be built -> reason printed, exit 1.
    fake3 = types.SimpleNamespace(script="", reason="no crash frames")
    monkeypatch.setattr("flight._repro.write_repro", lambda *a, **k: fake3)
    assert _cli._cmd_repro(ns) == 1


def test_cmd_diff_incomparable(monkeypatch):
    # 212: incomparable -> exit code 2.
    fake = types.SimpleNamespace(kind="incomparable", identical=False, render=lambda: "x")
    monkeypatch.setattr("flight._diff.diff_files", lambda l, r: fake)
    ns = argparse.Namespace(left="a", right="b")
    assert _cli._cmd_diff(ns) == 2


def test_cmd_debug_dap_server(tmp_path, monkeypatch):
    # 246-250: default (no --find/--list) starts the DAP server on stdio.
    p = _make_scope(tmp_path)
    called = {}
    monkeypatch.setattr("flight._dap.serve", lambda i, o, a: called.setdefault("served", True))
    ns = argparse.Namespace(file=str(p), find=None, list=False, limit=50)
    assert _cli._cmd_debug(ns) == 0
    assert called.get("served")


def test_cmd_trace_bad_file_and_links(tmp_path, capsys):
    # 269-270 (unreadable file skipped) and 285 (links printed).
    crash = _make_crash(tmp_path, "svc.flight", correlate=True, link=True)
    bogus = tmp_path / "bad.flight"
    bogus.write_bytes(b"not a flight file")
    ns = argparse.Namespace(paths=[str(crash), str(bogus)])
    rc = _cli._cmd_trace(ns)
    out = capsys.readouterr().out
    assert rc == 0
    assert "trace " in out
    assert "links to" in out


def test_cmd_ci_directory_no_crash(tmp_path):
    # 295-299: directory with no crash .flight -> message + exit 1.
    d = tmp_path / "empty"
    d.mkdir()
    ns = argparse.Namespace(file=str(d), output=None)
    assert _cli._cmd_ci(ns) == 1


def test_cmd_ci_directory_picks_crash(tmp_path):
    # 295-296, 300: directory with a crash .flight -> picked + rendered.
    crash = _make_crash(tmp_path, "cc.flight")
    d = tmp_path / "withcrash"
    d.mkdir()
    shutil.copy(crash, d / "cc.flight")
    ns = argparse.Namespace(file=str(d), output=None)
    assert _cli._cmd_ci(ns) == 0


def test_passphrase_sources(monkeypatch):
    # 311-320: --passphrase, then $FLIGHT_PASSPHRASE, then getpass prompt.
    assert _cli._passphrase(argparse.Namespace(passphrase="pw")) == "pw"

    ns = argparse.Namespace(passphrase=None)
    monkeypatch.setenv("FLIGHT_PASSPHRASE", "envpw")
    assert _cli._passphrase(ns) == "envpw"

    monkeypatch.delenv("FLIGHT_PASSPHRASE")
    monkeypatch.setattr("getpass.getpass", lambda *a, **k: "prompted")
    assert _cli._passphrase(ns) == "prompted"


def test_cmd_encrypt_success_and_error(tmp_path, monkeypatch):
    # 325-333: encrypt success, then a CryptoError path.
    crash = _make_crash(tmp_path, "enc.flight")
    ns = argparse.Namespace(file=str(crash), passphrase="pw", output=str(tmp_path / "enc.enc"))
    assert _cli._cmd_encrypt(ns) == 0
    assert (tmp_path / "enc.enc").exists()

    from flight._crypto import CryptoError

    def _raise(*a, **k):
        raise CryptoError("nope")

    monkeypatch.setattr("flight._crypto.encrypt_file", _raise)
    assert _cli._cmd_encrypt(ns) == 1


def test_cmd_decrypt_success_and_error(tmp_path, monkeypatch):
    # 338-346: decrypt success, then a CryptoError path.
    crash = _make_crash(tmp_path, "dec.flight")
    enc = tmp_path / "dec.enc"
    assert _cli._cmd_encrypt(argparse.Namespace(file=str(crash), passphrase="pw", output=str(enc))) == 0

    ns = argparse.Namespace(file=str(enc), passphrase="pw", output=str(tmp_path / "back.flight"))
    assert _cli._cmd_decrypt(ns) == 0

    from flight._crypto import CryptoError

    def _raise(*a, **k):
        raise CryptoError("nope")

    monkeypatch.setattr("flight._crypto.decrypt_file", _raise)
    assert _cli._cmd_decrypt(ns) == 1


def test_cmd_view_runs_viewer(tmp_path, monkeypatch):
    # 351-352, 360-361: viewer available -> run_viewer invoked.
    p = _make_scope(tmp_path)
    called = {}
    monkeypatch.setattr("flight._viewer.run", lambda f: called.setdefault("file", f))
    ns = argparse.Namespace(file=str(p))
    assert _cli._cmd_view(ns) == 0
    assert called.get("file") == str(p)


def test_cmd_view_missing_textual(tmp_path, monkeypatch):
    # 353-359: viewer import fails -> guidance + exit 1.
    monkeypatch.setitem(sys.modules, "flight._viewer", None)
    ns = argparse.Namespace(file=str(tmp_path / "whatever.flight"))
    assert _cli._cmd_view(ns) == 1
