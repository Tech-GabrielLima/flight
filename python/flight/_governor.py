"""Adaptive overhead governor — overhead as an SLO, not a bet (Phase 8).

The always-on recorder has an honest, *fixed* cost per event (~65 ns native;
see the bench). That is fine until a recorded function turns into a hot loop:
the same per-event cost, multiplied by millions of events a second, can push
overhead past what a production service will tolerate. A fixed setting forces a
bad choice — record everything and risk the tail latency, or record little and
miss the crash.

The governor removes the choice. It samples the recorder's event throughput on
a background thread, estimates the fraction of wall-clock the recording is
costing, and — if that estimate breaches the target ceiling — **demotes the
recording granularity one rung**, giving up line events first, then returns,
keeping only the call path and how it unwound (which is the load-bearing part of
a black box). When throughput falls back down it **promotes** again, up to the
granularity the user originally asked for. Overhead becomes a service-level
objective the recorder actively defends.

The decision logic (:class:`OverheadLadder`) is a pure state machine with
hysteresis, unit-tested by feeding it a sequence of overhead estimates. The
thread (:class:`Governor`) only samples a counter, does the arithmetic, and
calls an ``apply`` callback — so it is trivial to drive deterministically in a
test with an injected clock and stats source.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Callable, Optional

#: Recording granularity rungs, cheapest first. The governor never drops below
#: CALLS — "which functions ran and how the exception unwound" is the minimum
#: that still makes a useful black box.
LEVEL_CALLS = 0  # PY_START + RAISE/RERAISE/UNWIND only
LEVEL_RETURNS = 1  # + PY_RETURN
LEVEL_LINES = 2  # + LINE

LEVEL_NAMES = {LEVEL_CALLS: "calls", LEVEL_RETURNS: "returns", LEVEL_LINES: "lines"}


@dataclass
class OverheadLadder:
    """Pure decision state machine mapping overhead estimates → a level.

    Starts at ``baseline`` (the user's requested granularity) and only ever
    moves within ``[floor, baseline]``. Hysteresis stops it from flapping: it
    demotes after ``demote_after`` consecutive samples over the ceiling, and
    promotes after ``promote_after`` consecutive samples comfortably under it
    (below ``ceiling * promote_ratio``).
    """

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
        """Feed one overhead estimate (fraction, e.g. 0.05 = 5%); return the
        level to use now."""
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
            # In the comfortable band around the ceiling: hold, but let the
            # streak counters decay so a single spike doesn't trigger a move.
            self._over = 0
            self._under = 0
        return self.level

    def reset(self, level: Optional[int] = None) -> None:
        self.level = self.baseline if level is None else level
        self._over = 0
        self._under = 0


def estimate_overhead(events_delta: int, elapsed_s: float, per_event_ns: float) -> float:
    """Estimate the recording overhead as a fraction of wall-clock time.

    ``events_delta`` events, each costing ``per_event_ns`` nanoseconds, spread
    over ``elapsed_s`` seconds of wall time. This is a single-thread estimate —
    on many cores it overestimates (the cost is spread across cores), which errs
    on the safe side for an SLO. Honest and documented, not a promise.
    """
    if elapsed_s <= 0:
        return 0.0
    recording_ns = events_delta * per_event_ns
    return recording_ns / (elapsed_s * 1e9)


class Governor:
    """Background sampler that keeps recording overhead under a ceiling.

    Parameters
    ----------
    baseline:
        The granularity the user asked for (the ceiling of what we restore to).
    ceiling:
        Target overhead as a fraction (``0.03`` = 3%).
    interval:
        Sampling period in seconds.
    per_event_ns:
        Calibrated cost of one recorded event; the default matches the native
        hot path measured in the bench. Override to re-calibrate.
    stats_source:
        Callable returning a dict with ``total_events`` (defaults to
        ``flight._core.stats``). Injectable for tests.
    apply:
        Callable ``(level: int) -> None`` invoked when the level changes
        (defaults to retuning the live session). Injectable for tests.
    clock:
        Monotonic clock (defaults to ``time.monotonic``). Injectable for tests.
    """

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
        #: The most recent overhead estimate, for observability.
        self.last_estimate: float = 0.0

    # -- sampling (one step, pure enough to unit-test) ----------------------

    def _read_events(self) -> int:
        src = self._stats_source
        if src is None:
            from . import _core

            src = lambda: dict(_core.stats())  # noqa: E731
            self._stats_source = src
        try:
            return int(src().get("total_events", 0))
        except Exception:
            return self._last_events or 0

    def tick(self) -> int:
        """Take one sample, adjust the level if needed, return the level.

        Safe to call directly in tests (with injected stats/clock) instead of
        running the thread."""
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

    # -- thread lifecycle ---------------------------------------------------

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
        # Restore the user's requested granularity on the way out.
        self.ladder.reset()
        self._do_apply(self.ladder.baseline)


def _default_clock():
    import time

    return time.monotonic
