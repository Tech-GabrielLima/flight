"""Flight — a flight recorder for Python.

When a Python program dies, you shouldn't get a traceback and good luck — you
should get the complete black box of the flight: navigable, shareable and,
eventually, replayable in time.

Phase 0 (this release) records the execution's *rear-view mirror* — a ring of
the last thousands of PY_START / LINE / RETURN / RAISE events for your code —
and, on an uncaught exception, writes it to a ``.flight`` file. Inspect one
with ``python -m flight inspect crash.flight``.

Typical use::

    import flight
    flight.install()          # start recording
    ...                       # run your program
    # on an uncaught exception a .flight file is written automatically

Or wrap a script without touching it::

    python -m flight run myscript.py --args

See ``VISION.md`` for where this is going (locals & object graph in Phase 1,
a TUI viewer in Phase 1.5, time-travel in Phase 2).
"""

from __future__ import annotations

from ._adapters import Adapted, adapter
from ._config import Config
from ._ddmin import MinimizeResult, minimize
from ._diff import Divergence, diff_files as diff
from ._explain import Explanation, explain
from ._fingerprint import fingerprint
from ._install import dump, install, is_installed, uninstall
from ._nondet import ReplayDivergence, Tape, deterministic, replay, replay_tape
from ._read import Crash, Flight, Frame, Mutation, Recording, read
from ._record import record, watch
from ._timetravel import Step, TimeTravel

__version__ = "0.0.1"

__all__ = [
    "Adapted",
    "Config",
    "Crash",
    "Divergence",
    "Explanation",
    "Flight",
    "Frame",
    "MinimizeResult",
    "Mutation",
    "Recording",
    "ReplayDivergence",
    "Step",
    "Tape",
    "TimeTravel",
    "__version__",
    "adapter",
    "capture",
    "deterministic",
    "diff",
    "dump",
    "explain",
    "fingerprint",
    "install",
    "is_installed",
    "minimize",
    "read",
    "record",
    "replay",
    "replay_tape",
    "repro",
    "stats",
    "time_travel",
    "uninstall",
    "watch",
]


def time_travel(flight_path):
    """Open a scope `.flight` as a reverse debugger (:class:`TimeTravel`).

    Step backward and forward through the recorded state writes, and set a
    "breakpoint in the past" (``tt.find_first("running > 100")``). For an
    editor, `flight debug file.flight` exposes the same engine over DAP."""
    return TimeTravel(read(flight_path).recording())


def repro(flight_path, out_path=None, *, verify=True):
    """Generate (and verify) a standalone reproduction script from a crash
    `.flight`. Returns a `ReproResult`. See `flight repro` on the CLI."""
    from ._repro import write_repro

    return write_repro(flight_path, out_path, verify=verify)


def stats() -> dict:
    """Return recorder counters: total events, threads, codes, ring capacity."""
    from . import _core

    return dict(_core.stats())


def capture(path=None):
    """Write a `.flight` *now*, without waiting for an uncaught exception.

    If called while handling an exception, it captures the **full** black box
    for it — frames, locals, object graph, source, exception chain — exactly as
    the crash path would. Otherwise it writes a ring-only snapshot of the
    current execution. Handy inside an ``except`` block::

        try:
            process(request)
        except Exception:
            flight.capture()   # full crash detail for this handled error
            raise

    Returns the path written, or ``None`` on failure.
    """
    from ._capture import capture as _capture
    from ._config import Config
    from ._install import _active

    config = _active.config if _active is not None else Config()
    return _capture(config, path)
