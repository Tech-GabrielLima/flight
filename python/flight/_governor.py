from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Callable, Optional

LEVEL_CALLS = 0
LEVEL_RETURNS = 1
LEVEL_LINES = 2

LEVEL_NAMES = {LEVEL_CALLS: "calls", LEVEL_RETURNS: "returns", LEVEL_LINES: "lines"}


@dataclass
class OverheadLadder:

    baseline: int
    ceiling: float = 0.03
    floor: int = LEVEL_CALLS
    demote_after: int = 2
    promote_after: int = 4
    promote_ratio: float = 0.5

    def __post_init__(self):
        self.level = self.baseline
        self._over = 0
        self._under = 0

    def observe(self, est_overhead: float) -> int:
        if est_overhead > self.ceiling:
            self._over += 1
            self._under = 0
            if self._over >= self.demote_after and self.level > self.floor:
                self.level -= 1
                self._over = 0
        elif est_overhead < self.ceiling * self.promote_ratio:
            self._under += 1
            self._over = 0
            if self._under >= self.promote_after and self.level < self.baseline:
                self.level += 1
                self._under = 0
        else:
            self._over = 0
            self._under = 0
        return self.level

    def reset(self, level: Optional[int] = None) -> None:
        self.level = self.baseline if level is None else level
        self._over = 0
        self._under = 0


def estimate_overhead(events_delta: int, elapsed_s: float, per_event_ns: float) -> float:
    if elapsed_s <= 0:
        return 0.0
    recording_ns = events_delta * per_event_ns
    return recording_ns / (elapsed_s * 1e9)


class Governor:

    def __init__(
        self,
        baseline: int,
        *,
        ceiling: float = 0.03,
        interval: float = 0.5,
        per_event_ns: float = 65.0,
        floor: int = LEVEL_CALLS,
        stats_source: Optional[Callable[[], dict]] = None,
        apply: Optional[Callable[[int], None]] = None,
        clock: Optional[Callable[[], float]] = None,
        on_change: Optional[Callable[[int, int, float], None]] = None,
    ):
        self.ladder = OverheadLadder(baseline=baseline, ceiling=ceiling, floor=floor)
        self.interval = interval
        self.per_event_ns = per_event_ns
        self._stats_source = stats_source
        self._apply = apply
        self._clock = clock or _default_clock()
        self._on_change = on_change
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._last_events: Optional[int] = None
        self._last_t: Optional[float] = None
        self.last_estimate: float = 0.0


    def _read_events(self) -> int:
        src = self._stats_source
        if src is None:
            from . import _core

            src = lambda: dict(_core.stats())
            self._stats_source = src
        try:
            return int(src().get("total_events", 0))
        except Exception:
            return self._last_events or 0

    def tick(self) -> int:
        now = self._clock()
        events = self._read_events()
        if self._last_events is None or self._last_t is None:
            self._last_events, self._last_t = events, now
            return self.ladder.level
        delta = max(0, events - self._last_events)
        elapsed = now - self._last_t
        self._last_events, self._last_t = events, now
        self.last_estimate = estimate_overhead(delta, elapsed, self.per_event_ns)
        prev = self.ladder.level
        level = self.ladder.observe(self.last_estimate)
        if level != prev:
            self._do_apply(level)
            if self._on_change is not None:
                try:
                    self._on_change(prev, level, self.last_estimate)
                except Exception:
                    pass
        return level

    def _do_apply(self, level: int) -> None:
        apply = self._apply
        if apply is None:
            from ._install import _set_ring_level

            apply = _set_ring_level
        try:
            apply(level)
        except Exception:
            pass


    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="flight-governor", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                self.tick()
            except Exception:
                pass

    def stop(self) -> None:
        self._stop.set()
        t = self._thread
        if t is not None:
            t.join(timeout=self.interval * 4)
        self._thread = None
        self.ladder.reset()
        self._do_apply(self.ladder.baseline)


def _default_clock():
    import time

    return time.monotonic
