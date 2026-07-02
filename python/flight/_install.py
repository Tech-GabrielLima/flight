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
        events = ev.PY_START | ev.RAISE | ev.RERAISE | ev.PY_UNWIND
        if self.config.record_returns:
            events |= ev.PY_RETURN
        if self.config.record_lines:
            events |= ev.LINE
        _mon.set_events(TOOL_RING, events)
        self._installed = True

        import threading

        sys.excepthook = self._excepthook
        self._prev_threading_hook = threading.excepthook
        threading.excepthook = self._threading_excepthook
        self._prev_unraisable_hook = sys.unraisablehook
        sys.unraisablehook = self._unraisable_hook

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
    import platform

    cfg = config or (_active.config if _active is not None else Config())
    when_ms = int(time.time() * 1000)
    if path is None:
        path = cfg.crash_path(pid=_pid(), when_ms=when_ms)
    try:
        _core.dump(
            str(path),
            platform.python_version(),
            platform.platform(),
            list(sys.argv),
            _cwd(),
            _version(),
        )
        return path
    except Exception:
        return None


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
