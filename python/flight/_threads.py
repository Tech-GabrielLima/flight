from __future__ import annotations

import json
import sys
import threading

from ._nondet import ReplayDivergence, _current_channel

_ORDER_SOURCE = "threads.order"
_TURN_TIMEOUT = 10.0

_INTERNAL_LOCK_MODULES = frozenset(
    {
        "threading", "_thread", "asyncio", "concurrent", "queue", "logging",
        "selectors", "subprocess", "socket", "multiprocessing", "importlib",
        "weakref", "tempfile", "_pytest", "pytest",
    }
)


class _Channels:
    def __init__(self):
        self._n = 1
        self._lock = threading.Lock()

    def assign(self, thread) -> int:
        with self._lock:
            ch = self._n
            self._n += 1
        thread._flight_channel = ch
        return ch


class _ThreadBase:

    def __init__(self):
        self._chan = _Channels()
        self._saved: list = []
        self._orig_start = None

    def install(self) -> None:
        threading.current_thread()._flight_channel = 0
        self._orig_start = threading.Thread.start
        chan, orig = self._chan, self._orig_start

        def start(thread_self):
            chan.assign(thread_self)
            return orig(thread_self)

        threading.Thread.start = start
        self._install_locks()

    def _install_locks(self) -> None:
        raise NotImplementedError

    def _wrap_factory(self, attr: str, make_proxy) -> None:
        orig = getattr(threading, attr)

        def factory(*args, **kwargs):
            real = orig(*args, **kwargs)
            try:
                caller = sys._getframe(1).f_globals.get("__name__", "")
            except Exception:
                caller = ""
            if caller.split(".", 1)[0] in _INTERNAL_LOCK_MODULES:
                return real
            return make_proxy(real)

        setattr(threading, attr, factory)
        self._saved.append((threading, attr, orig))

    def uninstall(self) -> None:
        if self._orig_start is not None:
            threading.Thread.start = self._orig_start
            self._orig_start = None
        for obj, attr, orig in reversed(self._saved):
            try:
                setattr(obj, attr, orig)
            except Exception:
                pass
        self._saved.clear()


def _gated(blocking, timeout) -> bool:
    return bool(blocking) and timeout in (-1, None)


class _RecLock:
    def __init__(self, real, tracer):
        object.__setattr__(self, "_real", real)
        object.__setattr__(self, "_tracer", tracer)

    def acquire(self, blocking=True, timeout=-1):
        r = self._real.acquire(blocking, timeout)
        if r and _gated(blocking, timeout):
            self._tracer.on_acquire()
        return r

    def release(self):
        self._real.release()

    def locked(self):
        return self._real.locked()

    def __getattr__(self, name):
        return getattr(object.__getattribute__(self, "_real"), name)

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *_exc):
        self._real.release()
        return False


class ThreadRecorder(_ThreadBase):
    def __init__(self, recorder):
        super().__init__()
        self._rec = recorder
        self._order: list[int] = []
        self._order_lock = threading.Lock()

    def _install_locks(self) -> None:
        self._wrap_factory("Lock", lambda real: _RecLock(real, self))
        self._wrap_factory("RLock", lambda real: _RecLock(real, self))

    def on_acquire(self) -> None:
        ch = _current_channel()
        with self._order_lock:
            self._order.append(ch)

    def finalize(self) -> None:
        if self._order:
            self._rec.record_raw(_ORDER_SOURCE, "s", json.dumps(self._order))


class _ReplayLock:
    def __init__(self, real, tracer):
        object.__setattr__(self, "_real", real)
        object.__setattr__(self, "_tracer", tracer)

    def acquire(self, blocking=True, timeout=-1):
        if not _gated(blocking, timeout):
            return self._real.acquire(blocking, timeout)
        self._tracer.wait_turn()
        r = self._real.acquire(True, -1)
        self._tracer.advance()
        return r

    def release(self):
        self._real.release()

    def locked(self):
        return self._real.locked()

    def __getattr__(self, name):
        return getattr(object.__getattribute__(self, "_real"), name)

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *_exc):
        self._real.release()
        return False


class ThreadReplayer(_ThreadBase):
    def __init__(self, order):
        super().__init__()
        self._order = order or []
        self._i = 0
        self._cv = threading.Condition()

    def _install_locks(self) -> None:
        self._wrap_factory("Lock", lambda real: _ReplayLock(real, self))
        self._wrap_factory("RLock", lambda real: _ReplayLock(real, self))

    def wait_turn(self) -> None:
        ch = _current_channel()
        with self._cv:
            while self._i < len(self._order) and self._order[self._i] != ch:
                if not self._cv.wait(timeout=_TURN_TIMEOUT):
                    raise ReplayDivergence(
                        f"thread channel {ch}: timed out waiting for its turn to "
                        f"acquire a lock (recorded schedule head is "
                        f"{self._order[self._i] if self._i < len(self._order) else 'end'}); "
                        "the lock schedule diverged from the recording"
                    )
            if self._i >= len(self._order):
                raise ReplayDivergence(
                    f"thread channel {ch} acquired a lock, but the recorded "
                    "schedule is exhausted — more lock acquisitions than recorded"
                )

    def advance(self) -> None:
        with self._cv:
            self._i += 1
            self._cv.notify_all()
