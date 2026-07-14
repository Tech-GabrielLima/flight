from __future__ import annotations

import sys
import time
from typing import Optional

from . import _core
from ._config import Config

if not hasattr(sys, "monitoring"):
    raise RuntimeError("flight requires Python 3.12+ (sys.monitoring / PEP 669)")

_mon = sys.monitoring
TOOL_RING = 2
TOOL_SCOPE = 3

_active: Optional["_Session"] = None


class _Session:

    def __init__(self, config: Config):
        self.config = config
        self._prev_excepthook = sys.excepthook
        self._prev_threading_hook = None
        self._prev_unraisable_hook = None
        self._installed = False
        self._interesting: dict[str, bool] = {}
        self._scopes: dict[int, list] = {}
        self._scope_tool_on = False
        self._governor = None
        self._daemon = None
        self.baseline_level = self._baseline_level()


    def _baseline_level(self) -> int:
        from ._governor import LEVEL_CALLS, LEVEL_LINES, LEVEL_RETURNS

        if self.config.record_lines:
            return LEVEL_LINES
        if self.config.record_returns:
            return LEVEL_RETURNS
        return LEVEL_CALLS

    def _events_for_level(self, level: int) -> int:
        from ._governor import LEVEL_LINES, LEVEL_RETURNS

        ev = _mon.events
        mask = ev.PY_START | ev.RAISE | ev.RERAISE | ev.PY_UNWIND
        if level >= LEVEL_RETURNS and self.config.record_returns:
            mask |= ev.PY_RETURN
        if level >= LEVEL_LINES and self.config.record_lines:
            mask |= ev.LINE
        return mask

    def set_ring_level(self, level: int) -> None:
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


    def _excepthook(self, exc_type, exc_value, exc_tb):
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


    def install(self):
        _core.configure(self.config.ring_capacity)
        _core.configure_filter(
            list(self.config.deny_prefixes),
            list(self.config.force_include),
            _mon.DISABLE,
        )
        ev = _mon.events
        _mon.use_tool_id(TOOL_RING, "flight")
        _mon.register_callback(TOOL_RING, ev.PY_START, _core.cb_py_start)
        _mon.register_callback(TOOL_RING, ev.PY_RETURN, _core.cb_py_return)
        _mon.register_callback(TOOL_RING, ev.RAISE, _core.cb_raise)
        _mon.register_callback(TOOL_RING, ev.RERAISE, _core.cb_reraise)
        _mon.register_callback(TOOL_RING, ev.PY_UNWIND, _core.cb_unwind)
        _mon.register_callback(TOOL_RING, ev.LINE, _core.cb_line)
        _mon.set_events(TOOL_RING, self._events_for_level(self.baseline_level))
        self._installed = True

        import threading

        sys.excepthook = self._excepthook
        self._prev_threading_hook = threading.excepthook
        threading.excepthook = self._threading_excepthook
        self._prev_unraisable_hook = sys.unraisablehook
        sys.unraisablehook = self._unraisable_hook

        if self.config.overhead_slo is not None:
            self.start_governor()
        if self.config.daemon:
            self.start_daemon()


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
    global _active
    if _active is not None:
        _active.uninstall()
    cfg = config or Config()
    for k, v in overrides.items():
        setattr(cfg, k, v)
    if cfg.commit is True:
        from ._bisect import git_head

        cfg.commit = git_head()
    _active = _Session(cfg)
    _active.install()
    return cfg


def uninstall() -> None:
    global _active
    if _active is not None:
        _active.uninstall()
        _active = None


def is_installed() -> bool:
    return _active is not None


def dump(path=None, *, config: Optional[Config] = None):
    cfg = config or (_active.config if _active is not None else Config())
    when_ms = int(time.time() * 1000)
    if path is None:
        path = cfg.crash_path(pid=_pid(), when_ms=when_ms)
    return path if _write_ring_dump(path, cfg) else None


def _write_ring_dump(path, cfg: Config) -> bool:
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
    ctx = getattr(cfg, "correlation", None)
    if ctx is None:
        return []
    try:
        return list(ctx.to_nondet())
    except Exception:
        return []


def _set_ring_level(level: int) -> None:
    if _active is not None:
        _active.set_ring_level(level)


def start_daemon(**overrides):
    if _active is None:
        return None
    for k, v in overrides.items():
        setattr(_active.config, k, v)
    return _active.start_daemon()


def start_governor(**overrides):
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
