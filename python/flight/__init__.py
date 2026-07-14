from __future__ import annotations

from ._adapters import Adapted, adapter
from ._agent import AgentTools, FixResult, fix
from ._bisect import BisectResult, bisect_corpus, bisect_repro
from ._config import Config
from ._correlation import Link, TraceContext, trace_graph
from ._crypto import (
    CryptoError,
    CryptoUnavailable,
    DecryptError,
    decrypt_file,
    encrypt_file,
)
from ._ddmin import MinimizeResult, minimize
from ._diff import Divergence, diff_files as diff, diff_html
from ._explain import Explanation, explain
from ._fingerprint import fingerprint
from ._fleet import FleetIndex, Group, Record, report_to, safe_to_send
from ._generalize import Boundary, Generalization, generalize
from ._install import (
    correlate,
    dump,
    install,
    is_installed,
    link,
    start_daemon,
    start_governor,
    uninstall,
)
from ._nondet import ReplayDivergence, Tape, deterministic, replay, replay_tape
from ._read import Crash, Flight, Frame, Mutation, Recording, read
from ._record import record, watch
from ._slice import Hop, Slice, backward_slice
from ._timetravel import Step, TimeTravel
from ._web import FlightASGI, FlightWSGI
from ._whatif import Outcome, Override, WhatIf, run_whatif, what_if

__version__ = "0.0.3"

__all__ = [
    "Adapted",
    "AgentTools",
    "BisectResult",
    "Boundary",
    "Config",
    "Crash",
    "CryptoError",
    "CryptoUnavailable",
    "DecryptError",
    "Divergence",
    "Explanation",
    "FixResult",
    "FleetIndex",
    "Flight",
    "Generalization",
    "Group",
    "FlightASGI",
    "FlightWSGI",
    "Frame",
    "Hop",
    "Link",
    "MinimizeResult",
    "Mutation",
    "Record",
    "Outcome",
    "Override",
    "Recording",
    "ReplayDivergence",
    "Slice",
    "Step",
    "Tape",
    "TimeTravel",
    "TraceContext",
    "WhatIf",
    "__version__",
    "adapter",
    "backward_slice",
    "bisect_corpus",
    "bisect_repro",
    "capture",
    "correlate",
    "decrypt_file",
    "deterministic",
    "diff",
    "diff_html",
    "dump",
    "encrypt_file",
    "explain",
    "fingerprint",
    "fix",
    "generalize",
    "install",
    "is_installed",
    "link",
    "minimize",
    "read",
    "record",
    "replay",
    "replay_tape",
    "report_to",
    "repro",
    "run_whatif",
    "safe_to_send",
    "start_daemon",
    "start_governor",
    "stats",
    "time_travel",
    "trace_graph",
    "uninstall",
    "watch",
    "what_if",
    "why",
]


def time_travel(flight_path):
    return TimeTravel(read(flight_path).recording())


def why(flight_path, frame=0, var="", *, max_hops=32):
    return read(flight_path).why(frame=frame, var=var, max_hops=max_hops)


def repro(flight_path, out_path=None, *, verify=True):
    from ._repro import write_repro

    return write_repro(flight_path, out_path, verify=verify)


def stats() -> dict:
    from . import _core

    return dict(_core.stats())


def capture(path=None, *, correlation=None):
    from ._capture import capture as _capture
    from ._config import Config
    from ._install import _active

    config = _active.config if _active is not None else Config()
    return _capture(config, path, correlation=correlation)
