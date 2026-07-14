from __future__ import annotations

import os
import site
import sys
import sysconfig
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional


def _stdlib_and_site_prefixes() -> tuple[str, ...]:
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
    prefixes.add(os.path.realpath(str(Path(__file__).resolve().parent)))
    return tuple(sorted(p for p in prefixes if p))


@dataclass
class Config:

    ring_capacity: int = 4096
    output_dir: Path = field(default_factory=Path.cwd)
    dump_on_crash: bool = True
    record_lines: bool = False
    record_returns: bool = True

    capture_deadline_ms: int = 250
    capture_max_bytes: int = 20 * 1024 * 1024
    max_str: int = 10 * 1024
    max_container: int = 200
    max_depth: int = 6
    repr_limit: int = 200
    scrub_patterns: tuple[str, ...] = ()

    capture_max_mutations: int = 200_000
    deny_prefixes: tuple[str, ...] = field(default_factory=_stdlib_and_site_prefixes)
    force_include: tuple[str, ...] = ()

    overhead_slo: Optional[float] = None
    governor_interval: float = 0.5
    per_event_ns: float = 65.0
    daemon: bool = False
    daemon_interval: float = 1.0
    correlation: Any = None
    commit: Any = None

    def is_interesting(self, filename: str) -> bool:
        if not filename or filename.startswith("<"):
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

    def scope_path(self, pid: int, when_ms: int) -> Path:
        return self.output_dir / f"flight-scope-{pid}-{when_ms}.flight"
