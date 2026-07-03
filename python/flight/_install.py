"""Wiring Flight into a running interpreter via `sys.monitoring` (PEP 669).

The rear-view-mirror recording (the ring of PY_START / LINE / RETURN / RAISE
events) runs on **native Rust callbacks** registered directly with the
interpreter — no Python callback frame, no second FFI hop, no per-event Python
work. That is the difference between ~350 ns/event and a few tens of ns: a
Python callback that does *nothing* already costs ~110 ns just to dispatch, so
the only way below that is to leave Python out of the loop entirely. Filtering
(which code to record) and the ring live in Rust; see `flight._core`.

Phase-2 scope capture (which must read `frame.f_locals` in Python) runs on a
*second* monitoring tool, enabled only inside a `with flight.record()` block, so
its cost is never paid by the always-on recorder.

Everything obeys **P1 — primum non nocere**: native callbacks are
`catch_unwind`-guarded in Rust, Python callbacks swallow their errors, and
installation always preserves the previous excepthook behaviour.
"""

from __future__ import annotations

import sys
import time
from typing import Optional

from . import _core
from ._config import Config

if not hasattr(sys, "monitoring"):  # pragma: no cover - guarded by packaging
    raise RuntimeError("flight requires Python 3.12+ (sys.monitoring / PEP 669)")

_mon = sys.monitoring
#: Tool id for the always-on native ring recorder. 0 is debuggers, 1 coverage.
TOOL_RING = 2
#: Tool id for the Phase-2 scope capture (Python), active only inside a scope.
TOOL_SCOPE = 3

_active: Optional["_Session"] = None


class _Session:
    """Holds the installed state so it can be cleanly torn down."""

    def __init__(self, config: Config):
        self.config = config
        self._prev_excepthook = sys.excepthook
        self._prev_threading_hook = None
        self._prev_unraisable_hook = None
        self._installed = False
        # Per-filename interesting cache for the *scope* tool (the ring tool
        # filters in Rust). realpath is a filesystem hit we must not repeat.
        self._interesting: dict[str, bool] = {}
        # Phase-2 scope recording: a stack of active scopes per owning thread.
        self._scopes: dict[int, list] = {}
        self._scope_tool_on = False
        # Phase-8: adaptive governor + crash-surviving daemon (created lazily).
        self._governor = None
        self._daemon = None
        #: The user's requested granularity — the ceiling the governor restores
        #: to. Computed at install time from record_lines/record_returns.
        self.baseline_level = self._baseline_level()

    # -- ring granularity (Phase-8 governor retunes this live) --------------

    def _baseline_level(self) -> int:
        from ._governor import LEVEL_CALLS, LEVEL_LINES, LEVEL_RETURNS

        if self.config.record_lines:
            return LEVEL_LINES
        if self.config.record_returns:
            return LEVEL_RETURNS
        return LEVEL_CALLS

    def _events_for_level(self, level: int) -> int:
        """The `sys.monitoring` event mask for a granularity `level`, capped by
        the user's requested granularity (never records more than asked)."""
        from ._governor import LEVEL_LINES, LEVEL_RETURNS

        ev = _mon.events
        mask = ev.PY_START | ev.RAISE | ev.RERAISE | ev.PY_UNWIND
        if level >= LEVEL_RETURNS and self.config.record_returns:
            mask |= ev.PY_RETURN
        if level >= LEVEL_LINES and self.config.record_lines:
            mask |= ev.LINE
        return mask

    def set_ring_level(self, level: int) -> None:
        """Retune the always-on ring to a granularity `level` at runtime. Called
        by the overhead governor; safe to call any time after install."""
        if not self._installed:
            return
        level = max(0, min(level, self.baseline_level))
        try:
            _mon.set_events(TOOL_RING, self._events_for_level(level))
        except Exception:
            pass

    def _wanted(self, filename: str) -> bool:
        cached = self._interesting.get(filename)
        if cached is None:
            cached = self.config.is_interesting(filename)
            self._interesting[filename] = cached
        return cached

    # -- Phase-2 scope capture callbacks (tool 3, Python; scope-only) --------

    def _scope_on_line(self, code, line_number):
        try:
            if not self._wanted(code.co_filename):
                return _mon.DISABLE
            scope = self._current_scope()
            if scope is not None:
                scope.capture_line(code, line_number, sys._getframe(1))
        except Exception:
            pass
        return None

    def _scope_on_return(self, code, _offset, _retval):
        try:
            if self._wanted(code.co_filename):
                scope = self._current_scope()
                if scope is not None:
                    # A returning frame's last write has no trailing LINE event.
                    scope.capture_return(code, sys._getframe(1))
        except Exception:
            pass
        return None

    def _scope_on_unwind(self, code, _offset, _exc):
        try:
            if self._wanted(code.co_filename):
                scope = self._current_scope()
                if scope is not None:
                    scope.capture_return(code, sys._getframe(1))
        except Exception:
            pass
        return None

    # -- exception hooks ----------------------------------------------------

    def _excepthook(self, exc_type, exc_value, exc_tb):
        # Do our work first, then fall through to the original behaviour so a
        # user's own excepthook / default traceback still runs (P1).
        try:
            if self.config.dump_on_crash:
                from ._capture import write_crash_flight

                path = write_crash_flight(exc_type, exc_value, exc_tb, self.config)
                if path is not None:
                    print(f"[flight] recorded {path}", file=sys.stderr)
        except Exception:
            pass
        self._prev_excepthook(exc_type, exc_value, exc_tb)

    def _threading_excepthook(self, args):
        try:
            if self.config.dump_on_crash:
                from ._capture import write_crash_flight

                write_crash_flight(args.exc_type, args.exc_value, args.exc_traceback, self.config)
        except Exception:
            pass
        if self._prev_threading_hook is not None:
            self._prev_threading_hook(args)

    def _unraisable_hook(self, unraisable):
        # Exceptions Python can't propagate — e.g. raised in __del__, in a GC
        # callback, or in a weakref finalizer. The traceback may be None; the
        # capture still records the exception chain and the ring.
        try:
            if self.config.dump_on_crash and unraisable.exc_value is not None:
                from ._capture import write_crash_flight

                write_crash_flight(
                    unraisable.exc_type,
                    unraisable.exc_value,
                    unraisable.exc_traceback,
                    self.config,
                )
        except Exception:
            pass
        if self._prev_unraisable_hook is not None:
            self._prev_unraisable_hook(unraisable)

    # -- lifecycle ----------------------------------------------------------

    def install(self):
        _core.configure(self.config.ring_capacity)
        # Hand the deny/force policy and the DISABLE sentinel to Rust so the
        # native callbacks can filter and record without touching Python.
        _core.configure_filter(
            list(self.config.deny_prefixes),
            list(self.config.force_include),
            _mon.DISABLE,
        )
        ev = _mon.events
        _mon.use_tool_id(TOOL_RING, "flight")
        # Native Rust callbacks — the interpreter calls straight into Rust.
        _mon.register_callback(TOOL_RING, ev.PY_START, _core.cb_py_start)
        _mon.register_callback(TOOL_RING, ev.PY_RETURN, _core.cb_py_return)
        _mon.register_callback(TOOL_RING, ev.RAISE, _core.cb_raise)
        _mon.register_callback(TOOL_RING, ev.RERAISE, _core.cb_reraise)
        _mon.register_callback(TOOL_RING, ev.PY_UNWIND, _core.cb_unwind)
        _mon.register_callback(TOOL_RING, ev.LINE, _core.cb_line)
        # PY_START + the exception events are always on (the call path and how it
        # unwound). PY_RETURN and LINE are opt-outs/opt-ins for event volume.
        # The governor may dial this down/up later; start at the baseline.
        _mon.set_events(TOOL_RING, self._events_for_level(self.baseline_level))
        self._installed = True

        import threading

        sys.excepthook = self._excepthook
        self._prev_threading_hook = threading.excepthook
        threading.excepthook = self._threading_excepthook
        self._prev_unraisable_hook = sys.unraisablehook
        sys.unraisablehook = self._unraisable_hook

        # Phase-8: bring up the production machinery the config asked for.
        if self.config.overhead_slo is not None:
            self.start_governor()
        if self.config.daemon:
            self.start_daemon()

    # -- Phase-8 lifecycle --------------------------------------------------

    def start_governor(self):
        from ._governor import Governor

        if self._governor is not None:
            return self._governor
        ceiling = self.config.overhead_slo if self.config.overhead_slo is not None else 0.03
        gov = Governor(
            baseline=self.baseline_level,
            ceiling=ceiling,
            interval=self.config.governor_interval,
            per_event_ns=self.config.per_event_ns,
            apply=self.set_ring_level,
        )
        gov.start()
        self._governor = gov
        return gov

    def start_daemon(self):
        from ._daemon import Daemon

        if self._daemon is not None:
            return self._daemon
        d = Daemon(self.config, interval=self.config.daemon_interval).start()
        self._daemon = d
        return d

    # -- Phase-2 scope stack (tool 3) ---------------------------------------

    def _enter_scope(self, scope):
        first = not self._scopes
        self._scopes.setdefault(scope.owner, []).append(scope)
        if first:
            self._enable_scope_tool()

    def _exit_scope(self, scope):
        stack = self._scopes.get(scope.owner)
        if stack and scope in stack:
            stack.remove(scope)
            if not stack:
                del self._scopes[scope.owner]
        if not self._scopes:
            self._disable_scope_tool()

    def _enable_scope_tool(self):
        ev = _mon.events
        _mon.use_tool_id(TOOL_SCOPE, "flight-scope")
        _mon.register_callback(TOOL_SCOPE, ev.LINE, self._scope_on_line)
        _mon.register_callback(TOOL_SCOPE, ev.PY_RETURN, self._scope_on_return)
        _mon.register_callback(TOOL_SCOPE, ev.PY_UNWIND, self._scope_on_unwind)
        _mon.set_events(TOOL_SCOPE, ev.LINE | ev.PY_RETURN | ev.PY_UNWIND)
        self._scope_tool_on = True

    def _disable_scope_tool(self):
        if not self._scope_tool_on:
            return
        try:
            _mon.set_events(TOOL_SCOPE, 0)
            for event in (_mon.events.LINE, _mon.events.PY_RETURN, _mon.events.PY_UNWIND):
                _mon.register_callback(TOOL_SCOPE, event, None)
            _mon.free_tool_id(TOOL_SCOPE)
        except Exception:
            pass
        self._scope_tool_on = False

    def _current_scope(self):
        import threading

        stack = self._scopes.get(threading.get_ident())
        return stack[-1] if stack else None

    def uninstall(self):
        if not self._installed:
            return
        if self._governor is not None:
            try:
                self._governor.stop()
            except Exception:
                pass
            self._governor = None
        if self._daemon is not None:
            try:
                self._daemon.stop(clean=True)
            except Exception:
                pass
            self._daemon = None
        self._disable_scope_tool()
        try:
            _mon.set_events(TOOL_RING, 0)
            for event in (
                _mon.events.PY_START,
                _mon.events.PY_RETURN,
                _mon.events.LINE,
                _mon.events.RAISE,
                _mon.events.RERAISE,
                _mon.events.PY_UNWIND,
            ):
                _mon.register_callback(TOOL_RING, event, None)
            _mon.free_tool_id(TOOL_RING)
        except Exception:
            pass

        import threading

        sys.excepthook = self._prev_excepthook
        if self._prev_threading_hook is not None:
            threading.excepthook = self._prev_threading_hook
        if self._prev_unraisable_hook is not None:
            sys.unraisablehook = self._prev_unraisable_hook
        _core.reset()
        self._installed = False


def install(config: Optional[Config] = None, **overrides) -> Config:
    """Start recording. Returns the active :class:`Config`.

    Idempotent-ish: installing while already installed replaces the session
    (the previous one is uninstalled first).
    """
    global _active
    if _active is not None:
        _active.uninstall()
    cfg = config or Config()
    for k, v in overrides.items():
        setattr(cfg, k, v)
    _active = _Session(cfg)
    _active.install()
    return cfg


def uninstall() -> None:
    """Stop recording and restore the interpreter to its prior state."""
    global _active
    if _active is not None:
        _active.uninstall()
        _active = None


def is_installed() -> bool:
    return _active is not None


def dump(path=None, *, config: Optional[Config] = None):
    """Write the current recording to a `.flight` file.

    Returns the path written, or ``None`` if writing failed (P1: never raise
    into a crashing program). If `path` is omitted a timestamped name in the
    session's output directory is used.
    """
    cfg = config or (_active.config if _active is not None else Config())
    when_ms = int(time.time() * 1000)
    if path is None:
        path = cfg.crash_path(pid=_pid(), when_ms=when_ms)
    return path if _write_ring_dump(path, cfg) else None


def _write_ring_dump(path, cfg: Config) -> bool:
    """Write a ring-only `.flight`, embedding the trace context if one is set.

    Correlation rides on the NONDET tape, so a plain ring dump uses `_core.dump`
    while a correlated one uses `_core.dump_nondet` (which lays down the same
    META + EVENT_RING plus the NONDET block). Returns True on success."""
    import platform

    meta = (
        platform.python_version(),
        platform.platform(),
        list(sys.argv),
        _cwd(),
        _version(),
    )
    entries = _correlation_entries(cfg)
    try:
        if entries:
            _core.dump_nondet(str(path), *meta, entries, [])
        else:
            _core.dump(str(path), *meta)
        return True
    except Exception:
        return False


def _correlation_entries(cfg: Config):
    """NONDET entries encoding the config's trace context, or ``[]``."""
    ctx = getattr(cfg, "correlation", None)
    if ctx is None:
        return []
    try:
        return list(ctx.to_nondet())
    except Exception:
        return []


def _set_ring_level(level: int) -> None:
    """Retune the active session's ring granularity (governor callback)."""
    if _active is not None:
        _active.set_ring_level(level)


def start_daemon(**overrides):
    """Start the crash-surviving supervisor for the active session (Phase 8).

    Returns the :class:`~flight._daemon.Daemon`, or ``None`` if Flight is not
    installed. Idempotent — starting twice returns the running daemon."""
    if _active is None:
        return None
    for k, v in overrides.items():
        setattr(_active.config, k, v)
    return _active.start_daemon()


def start_governor(**overrides):
    """Start the adaptive overhead governor for the active session (Phase 8).

    Pass ``overhead_slo=0.03`` (or set it on the Config) to choose the ceiling.
    Returns the :class:`~flight._governor.Governor`, or ``None`` if Flight is
    not installed."""
    if _active is None:
        return None
    for k, v in overrides.items():
        setattr(_active.config, k, v)
    if _active.config.overhead_slo is None:
        _active.config.overhead_slo = 0.03
    return _active.start_governor()


def correlate(
    traceparent=None,
    *,
    service=None,
    trace_state="",
    from_env=True,
    from_otel=False,
    root=False,
):
    """Stamp a distributed-trace context on every black box this process writes.

    Resolves the context from (in order) an explicit ``traceparent``, a live
    OpenTelemetry span (``from_otel``), or the environment (``from_env``); with
    ``root=True`` it mints a fresh root when nothing is found. Returns the
    :class:`~flight._correlation.TraceContext`, or ``None`` if unresolved."""
    from ._correlation import TraceContext, resolve

    ctx = resolve(
        traceparent=traceparent,
        service=service,
        trace_state=trace_state,
        from_env=from_env,
        from_otel=from_otel,
    )
    if ctx is None and root:
        ctx = TraceContext.new_root(service=service)
    cfg = _active.config if _active is not None else None
    if cfg is not None:
        cfg.correlation = ctx
    return ctx


def link(ref, *, service=None, trace_id=None):
    """Record a link from this process's black boxes to another `.flight`.

    ``ref`` is a `.flight` path (or a Flight/summary with a ``.path``). The link
    is added to the current trace context (a root is minted if none is set), so
    the referenced black box shows up in the cross-service crash graph."""
    from ._correlation import Link, TraceContext

    cfg = _active.config if _active is not None else None
    if cfg is None:
        return None
    ctx = getattr(cfg, "correlation", None)
    if ctx is None:
        ctx = TraceContext.new_root(service=service)
    ref_path = getattr(ref, "path", ref)
    tid = trace_id if trace_id is not None else ctx.trace_id
    cfg.correlation = ctx.with_link(Link(trace_id=tid, ref=str(ref_path), service=service))
    return cfg.correlation


def _pid() -> int:
    import os

    return os.getpid()


def _cwd() -> str:
    import os

    try:
        return os.getcwd()
    except Exception:
        return ""


def _version() -> str:
    from . import __version__

    return __version__
