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

from ._config import Config
from ._install import dump, install, is_installed, uninstall
from ._read import Flight, read

__version__ = "0.0.1"

__all__ = [
    "Config",
    "Flight",
    "__version__",
    "capture",
    "dump",
    "install",
    "is_installed",
    "read",
    "stats",
    "uninstall",
]


def stats() -> dict:
    """Return recorder counters: total events, threads, codes, ring capacity."""
    from . import _core

    return dict(_core.stats())


def capture(path=None):
    """Write a `.flight` of the recording *now*, without waiting for a crash.

    Handy inside an ``except`` block for suspicious-but-handled errors::

        try:
            process(request)
        except Exception:
            flight.capture()
            raise

    Returns the path written, or ``None`` on failure.
    """
    return dump(path)
