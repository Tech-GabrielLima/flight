from __future__ import annotations

import json

import asyncio
import asyncio.events

from ._nondet import ReplayDivergence

_ORDER_SOURCE = "asyncio.order"


class _TaskOrder:

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

    def __init__(self, recorder):
        super().__init__()
        self._rec = recorder

    def finalize(self) -> None:
        if self.completed:
            self._rec.record_raw(_ORDER_SOURCE, "s", json.dumps(self.completed))


class AsyncioReplayer(_TaskOrder):

    def __init__(self, tape):
        super().__init__()
        payload = tape.pop_control(_ORDER_SOURCE)
        self._expected = None if payload is None else json.loads(payload)

    def finalize(self) -> None:
        if self._expected is None:
            return
        if self.completed != self._expected:
            raise ReplayDivergence(
                "asyncio task completion order diverged from the recording: "
                f"recorded {self._expected}, replayed {self.completed} — the "
                "scheduler took a different path (an un-replayed source of "
                "non-determinism)"
            )
