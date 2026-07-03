"""Phase 4b — deterministic replay of thread scheduling.

Under the GIL, Python bytecode is already serialized; what still varies run to
run is **the order in which threads acquire shared locks** — and that order is
exactly what turns a lock-protected data structure's *contents* non-deterministic
(which thread appended first, which writer won the race). A program is a
deterministic function of its inputs *and the order the scheduler granted those
locks*. So we record that order and, on replay, **enforce it**: each thread waits
for its recorded turn before a lock acquisition proceeds.

**Model.** Threads are numbered in start order (`_flight_channel`, stamped on the
Thread; the scope's own thread is channel 0), so each thread replays its own
boundary calls on its own tape lane (see `_nondet._current_channel`). Blocking
`threading.Lock`/`RLock` acquisitions made through the module factories inside
the scope are logged as a global sequence of channel ids; on replay a thread
blocks until the head of that sequence is its channel, then acquires the real
lock and advances the sequence. Only *blocking, untimed* acquisitions are gated
(the `with lock:` case); non-blocking / timed tries pass through untouched.

**Honest scope.** This reproduces the *lock-acquisition schedule* — the cause of
the classic flaky "which thread won" bug. It does **not** capture data races on
unlocked shared state (genuinely outside any lock-based record/replay), and only
locks created *inside* the scope via the factories are tracked. A safety timeout
turns a replay deadlock into a `ReplayDivergence` instead of a hang (P1).
"""

from __future__ import annotations

import json
import sys
import threading

from ._nondet import ReplayDivergence, _current_channel

_ORDER_SOURCE = "threads.order"
#: Seconds a replaying thread waits for its turn before declaring divergence.
_TURN_TIMEOUT = 10.0

#: Locks created *by these modules* are left untouched — they are the runtime's
#: own machinery (threading's Condition/Event/Thread bootstrap use module-level
#: `Lock`/`RLock`, and must keep their full private API and never be gated, or
#: the interpreter's own synchronization would deadlock on replay). We track
#: only locks the user's code creates.
_INTERNAL_LOCK_MODULES = frozenset(
    {
        "threading", "_thread", "asyncio", "concurrent", "queue", "logging",
        "selectors", "subprocess", "socket", "multiprocessing", "importlib",
        "weakref", "tempfile", "_pytest", "pytest",
    }
)


class _Channels:
    def __init__(self):
        self._n = 1  # channel 0 is reserved for the scope's own thread
        self._lock = threading.Lock()

    def assign(self, thread) -> int:
        with self._lock:
            ch = self._n
            self._n += 1
        thread._flight_channel = ch
        return ch


class _ThreadBase:
    """Shared install/uninstall: number threads and wrap the lock factories."""

    def __init__(self):
        self._chan = _Channels()
        self._saved: list = []
        self._orig_start = None

    def install(self) -> None:
        # The thread entering the scope is channel 0; started threads get 1, 2…
        threading.current_thread()._flight_channel = 0
        self._orig_start = threading.Thread.start
        chan, orig = self._chan, self._orig_start

        def start(thread_self):
            chan.assign(thread_self)  # numbered in start order (deterministic)
            return orig(thread_self)

        threading.Thread.start = start
        self._install_locks()

    def _install_locks(self) -> None:  # pragma: no cover - overridden
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
                return real  # runtime machinery — leave it fully intact
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
    """Only plain `with lock:` style acquisitions are ordered: blocking, untimed.
    Non-blocking or timed tries are passed through (and never recorded)."""
    return bool(blocking) and timeout in (-1, None)


# -- record -----------------------------------------------------------------


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

    def __getattr__(self, name):  # delegate the private lock API (Condition etc.)
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


# -- replay -----------------------------------------------------------------


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
