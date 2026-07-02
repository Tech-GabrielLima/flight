"""Configuration and the module allow/deny policy.

The recorder must not drown in stdlib/site-packages line events — those are
rarely what a user is debugging and they dominate the cost (guide §0.1). By
default we record only code that lives *outside* the standard library and
installed packages, plus we always exclude Flight itself.
"""

from __future__ import annotations

import os
import site
import sys
import sysconfig
from dataclasses import dataclass, field
from pathlib import Path


def _stdlib_and_site_prefixes() -> tuple[str, ...]:
    """Directory prefixes whose code is *not* recorded by default."""
    prefixes: set[str] = set()
    for key in ("stdlib", "platstdlib", "purelib", "platlib"):
        try:
            p = sysconfig.get_paths().get(key)
            if p:
                prefixes.add(os.path.realpath(p))
        except Exception:
            pass
    try:
        for p in site.getsitepackages():
            prefixes.add(os.path.realpath(p))
    except Exception:
        pass
    try:
        prefixes.add(os.path.realpath(site.getusersitepackages()))
    except Exception:
        pass
    # Flight's own package directory — never record ourselves.
    prefixes.add(os.path.realpath(str(Path(__file__).resolve().parent)))
    return tuple(sorted(p for p in prefixes if p))


@dataclass
class Config:
    """Runtime configuration for a recording session."""

    #: Per-thread ring capacity (events). Rounded up to a power of two.
    ring_capacity: int = 4096
    #: Directory for auto-dumped `.flight` files on an uncaught exception.
    output_dir: Path = field(default_factory=Path.cwd)
    #: Whether to auto-dump on an uncaught exception.
    dump_on_crash: bool = True
    #: Record a LINE event for every source line executed. This is the finest
    #: granularity but the most expensive: `sys.monitoring` calls back into
    #: Python once per line, so line-heavy code slows down substantially until
    #: the callback is moved into native code (a planned Phase-1 optimization).
    #: Off by default — the always-on black box records call/return/exception
    #: granularity, which is cheap and already answers "which functions ran,
    #: and how did the exception unwind?".
    record_lines: bool = False
    #: Directory prefixes to exclude from recording.
    deny_prefixes: tuple[str, ...] = field(default_factory=_stdlib_and_site_prefixes)
    #: Extra path substrings to always record even if under a denied prefix.
    force_include: tuple[str, ...] = ()

    def is_interesting(self, filename: str) -> bool:
        """True if code from `filename` should be recorded."""
        if not filename or filename.startswith("<"):
            # Synthetic code: <frozen ...>, <string>, REPL input, etc.
            return False
        real = os.path.realpath(filename)
        for inc in self.force_include:
            if inc in real:
                return True
        for deny in self.deny_prefixes:
            if real.startswith(deny):
                return False
        return True

    def crash_path(self, pid: int, when_ms: int) -> Path:
        return self.output_dir / f"flight-{pid}-{when_ms}.flight"
