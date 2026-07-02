"""Wiring Flight into a running interpreter via `sys.monitoring` (PEP 669).

Phase 0 records the *rear-view mirror*: a ring of PY_START / LINE / RETURN /
RAISE events for user code, and, on an uncaught exception, dumps it to a
`.flight` file. Locals, frames and the object graph are Phase 1.

Everything here obeys **P1 — primum non nocere**: every callback is wrapped so
it can never raise into the interpreter, and installation always preserves the
previous excepthook behaviour.
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
#: Flight's monitoring tool id. 0 is debuggers, 1 is coverage; 2 is free.
TOOL_ID = 2

_active: Optional["_Session"] = None


class _Session:
    """Holds the installed state so it can be cleanly torn down."""

    def __init__(self, config: Config):
        self.config = config
        self._prev_excepthook = sys.excepthook
        self._prev_threading_hook = None
        self._installed = False
        # Memoize the record/skip decision per source filename. `is_interesting`
        # calls os.path.realpath — a filesystem hit we must not pay per LINE
        # event (P2). Filenames are interned per code object, so a dict keyed
        # on the filename string is a cheap, stable cache.
        self._interesting: dict[str, bool] = {}

    def _wanted(self, filename: str) -> bool:
        cached = self._interesting.get(filename)
        if cached is None:
            cached = self.config.is_interesting(filename)
            self._interesting[filename] = cached
        return cached

    def _note_code(self, code) -> None:
        """Register a code object's identity (first-write-wins)."""
        _core.register_code(id(code), code.co_filename, code.co_qualname, code.co_firstlineno)

    # -- sys.monitoring callbacks (hot path; must never raise) --------------

    def _on_py_start(self, code, _offset):
        try:
            if not self._wanted(code.co_filename):
                # Uninteresting code: stop PY_START here. LINE events for it
                # are silenced the first time each line is hit (_on_line).
                return _mon.DISABLE
            self._note_code(code)
            _core.record(_core.EVENT_PY_START, id(code), code.co_firstlineno)
        except Exception:
            pass
        return None

    def _on_line(self, code, line_number):
        try:
            if not self._wanted(code.co_filename):
                # Pay once per uninteresting location, then never again.
                return _mon.DISABLE
            # PY_START already registered interesting code; recording here is
            # kept to just the ring push to hold the per-line cost down.
            _core.record(_core.EVENT_LINE, id(code), line_number)
        except Exception:
            pass
        return None

    def _on_py_return(self, code, _offset, _retval):
        try:
            if self._wanted(code.co_filename):
                _core.record(_core.EVENT_PY_RETURN, id(code), 0)
        except Exception:
            pass
        return None

    def _on_raise(self, code, _offset, _exc):
        try:
            if self._wanted(code.co_filename):
                self._note_code(code)  # rare event: ensure the frame has a name
                _core.record(_core.EVENT_RAISE, id(code), 0)
        except Exception:
            pass
        return None

    def _on_reraise(self, code, _offset, _exc):
        try:
            if self._wanted(code.co_filename):
                self._note_code(code)
                _core.record(_core.EVENT_RERAISE, id(code), 0)
        except Exception:
            pass
        return None

    def _on_unwind(self, code, _offset, _exc):
        try:
            if self._wanted(code.co_filename):
                self._note_code(code)
                _core.record(_core.EVENT_PY_UNWIND, id(code), 0)
        except Exception:
            pass
        return None

    # -- exception hooks ----------------------------------------------------

    def _excepthook(self, exc_type, exc_value, exc_tb):
        # Do our work first, then fall through to the original behaviour so a
        # user's own excepthook / default traceback still runs (P1).
        try:
            if self.config.dump_on_crash:
                path = dump(config=self.config)
                if path is not None:
                    print(f"[flight] recorded {path}", file=sys.stderr)
        except Exception:
            pass
        self._prev_excepthook(exc_type, exc_value, exc_tb)

    def _threading_excepthook(self, args):
        try:
            if self.config.dump_on_crash:
                dump(config=self.config)
        except Exception:
            pass
        if self._prev_threading_hook is not None:
            self._prev_threading_hook(args)

    # -- lifecycle ----------------------------------------------------------

    def install(self):
        _core.configure(self.config.ring_capacity)
        _mon.use_tool_id(TOOL_ID, "flight")
        ev = _mon.events
        _mon.register_callback(TOOL_ID, ev.PY_START, self._on_py_start)
        _mon.register_callback(TOOL_ID, ev.PY_RETURN, self._on_py_return)
        _mon.register_callback(TOOL_ID, ev.RAISE, self._on_raise)
        _mon.register_callback(TOOL_ID, ev.RERAISE, self._on_reraise)
        _mon.register_callback(TOOL_ID, ev.PY_UNWIND, self._on_unwind)
        events = ev.PY_START | ev.PY_RETURN | ev.RAISE | ev.RERAISE | ev.PY_UNWIND
        if self.config.record_lines:
            _mon.register_callback(TOOL_ID, ev.LINE, self._on_line)
            events |= ev.LINE
        _mon.set_events(TOOL_ID, events)

        import threading

        sys.excepthook = self._excepthook
        self._prev_threading_hook = threading.excepthook
        threading.excepthook = self._threading_excepthook
        self._installed = True

    def uninstall(self):
        if not self._installed:
            return
        try:
            _mon.set_events(TOOL_ID, 0)
            for event in (
                _mon.events.PY_START,
                _mon.events.PY_RETURN,
                _mon.events.LINE,
                _mon.events.RAISE,
                _mon.events.RERAISE,
                _mon.events.PY_UNWIND,
            ):
                _mon.register_callback(TOOL_ID, event, None)
            _mon.free_tool_id(TOOL_ID)
        except Exception:
            pass

        import threading

        sys.excepthook = self._prev_excepthook
        if self._prev_threading_hook is not None:
            threading.excepthook = self._prev_threading_hook
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
