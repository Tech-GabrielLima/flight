"""Phase 4a — asyncio scheduling order (a divergence detector).

A single-loop asyncio program's non-determinism comes from *time* and *I/O* —
which order tasks become runnable in. :mod:`_nondet` and :mod:`_io` already
replay those, so on replay the loop schedules the same way. What remains is to
*prove* it: record the order tasks completed in, and on replay verify it matched.
If anything still diverges (a boundary we don't yet interpose), this pinpoints it
at the task level instead of letting the replay silently drift.

Only public API is used — ``loop.set_task_factory`` and ``Task.add_done_callback``
— installed by wrapping event-loop creation. Everything is guarded (P1): if a
CPython build arranges asyncio differently, verification is simply skipped, never
fatal. Per-await (sub-task) ordering is a later phase (native loop control).

The completion order is stored as a single tape entry (``asyncio.order``) written
when the scope closes, so it never interleaves with the fine-grained I/O cursor.
"""

from __future__ import annotations

import json

# Import asyncio eagerly, at module import time. This module is imported by
# `_Deterministic.__init__` *before* the scalar boundaries are interposed, so
# asyncio's own one-time import side effects (its chain pulls in `logging`,
# whose module body calls `time.time_ns()`) run uninterposed and never leak into
# the tape — otherwise a first-import-inside-the-scope would record a clock read
# that a same-process replay (modules already cached) would not reproduce.
import asyncio  # noqa: E402  (eager on purpose)
import asyncio.events  # noqa: E402

from ._nondet import ReplayDivergence

_ORDER_SOURCE = "asyncio.order"


class _TaskOrder:
    """Shared base: patches loop creation to install a completion-order factory."""

    def __init__(self):
        self._chan = 0
        self.completed: list[int] = []
        self._saved: list = []

    def _factory(self, loop, coro, **kwargs):
        import asyncio

        cid = self._chan
        self._chan += 1
        try:
            task = asyncio.Task(coro, loop=loop, **kwargs)
        except TypeError:
            # Older/newer signature: fall back to the minimal form.
            task = asyncio.Task(coro, loop=loop)
        task.add_done_callback(lambda _t, c=cid: self.completed.append(c))
        return task

    def install(self) -> None:
        import asyncio
        import asyncio.events as events

        for mod, attr in ((asyncio, "new_event_loop"), (events, "new_event_loop")):
            try:
                orig = getattr(mod, attr)
            except AttributeError:
                continue
            setattr(mod, attr, self._wrap_new_loop(orig))
            self._saved.append((mod, attr, orig))

    def _wrap_new_loop(self, orig):
        def new_event_loop(*args, **kwargs):
            loop = orig(*args, **kwargs)
            try:
                loop.set_task_factory(self._factory)
            except Exception:
                pass
            return loop

        return new_event_loop

    def uninstall(self) -> None:
        for mod, attr, orig in reversed(self._saved):
            try:
                setattr(mod, attr, orig)
            except Exception:
                pass
        self._saved.clear()


class AsyncioRecorder(_TaskOrder):
    """Records the task-completion order into the tape when the scope closes."""

    def __init__(self, recorder):
        super().__init__()
        self._rec = recorder

    def finalize(self) -> None:
        if self.completed:
            self._rec.record_raw(_ORDER_SOURCE, "s", json.dumps(self.completed))


class AsyncioReplayer(_TaskOrder):
    """Observes the task-completion order and checks it against the recording."""

    def __init__(self, tape):
        super().__init__()
        self._tape = tape

    def finalize(self) -> None:
        if self._tape.peek_source() != _ORDER_SOURCE:
            return
        _tag, payload = self._tape.take_raw(_ORDER_SOURCE)
        expected = json.loads(payload)
        if self.completed != expected:
            raise ReplayDivergence(
                "asyncio task completion order diverged from the recording: "
                f"recorded {expected}, replayed {self.completed} — the scheduler "
                "took a different path (an un-replayed source of non-determinism)"
            )
