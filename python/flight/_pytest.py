"""A pytest plugin: when a test fails, attach its black box (Phase 9).

A traceback tells you *where* a test blew up; a `.flight` tells you the whole
story — every function that ran, the locals at the crash, the object graph, the
exception chain — captured the moment it happened, with nothing to reproduce.
This plugin records each test under Flight and, on failure, writes a `.flight`
named after the test node, then points at it in the failure report.

It is **opt-in** (nothing happens without ``--flight``) and **never changes the
outcome of a test** (P1): the recording is wrapped so a failure inside Flight
can only mean "no black box for this one", never a spuriously failing or erroring
test. Recording is installed only for the duration of each test's call phase, so
the black box holds that test's execution and nothing else.

Enable it explicitly::

    pytest --flight                 # write a .flight for every failing test
    pytest --flight --flight-dir=artifacts/flight
    pytest --flight --flight-lines  # per-line granularity (finer, costlier)

Registered as a ``pytest11`` entry point, so an installed Flight is discovered
automatically; the flag keeps it dormant until you ask for it.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

# Where we stash the written path on the item, so the report hook can find it.
_PATH_KEY = pytest.StashKey[str]()

_SANITIZE = re.compile(r"[^A-Za-z0-9_.-]+")


def _safe_name(nodeid: str, max_len: int = 120) -> str:
    """Turn a pytest node id into a filesystem-safe basename."""
    name = _SANITIZE.sub("_", nodeid).strip("_")
    if len(name) > max_len:
        # Keep the tail (the test name / params) which is the distinctive part.
        name = name[-max_len:].lstrip("_")
    return name or "test"


# -- options ---------------------------------------------------------------


def pytest_addoption(parser):
    group = parser.getgroup("flight", "Flight recorder")
    group.addoption(
        "--flight",
        action="store_true",
        default=False,
        help="write a .flight black box for each failing test",
    )
    group.addoption(
        "--flight-dir",
        action="store",
        default=".flight",
        metavar="DIR",
        help="directory for the .flight files (default: .flight/)",
    )
    group.addoption(
        "--flight-lines",
        action="store_true",
        default=False,
        help="record per-line events (finest granularity, higher overhead)",
    )
    group.addoption(
        "--flight-all",
        action="store_true",
        default=False,
        help="also write a .flight for tests that pass (not just failures)",
    )


def pytest_configure(config):
    config._flight_enabled = bool(config.getoption("--flight"))
    config._flight_written = []  # list[(nodeid, path)] for the summary
    if config._flight_enabled:
        out = Path(config.getoption("--flight-dir"))
        try:
            out.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        config._flight_dir = out


# -- the recording wrapper --------------------------------------------------


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item):
    """Record the test's call phase; on an exception, write its black box.

    A hookwrapper sees the exception the test raised (``outcome.excinfo``)
    without swallowing it — the test still fails exactly as it would have.
    """
    config = item.config
    if not getattr(config, "_flight_enabled", False):
        yield
        return

    import flight

    installed = False
    try:
        flight.install(
            output_dir=Path(config._flight_dir),
            record_lines=bool(config.getoption("--flight-lines")),
        )
        installed = True
    except Exception:
        # If we can't even install, run the test unrecorded — never block it.
        yield
        return

    outcome = None
    try:
        outcome = yield
    finally:
        excinfo = getattr(outcome, "excinfo", None) if outcome is not None else None
        try:
            want = excinfo is not None or bool(config.getoption("--flight-all"))
            if installed and want:
                path = _write_for(item, excinfo)
                if path is not None:
                    item.stash[_PATH_KEY] = str(path)
                    config._flight_written.append((item.nodeid, str(path)))
        except Exception:
            pass
        if installed:
            try:
                flight.uninstall()
            except Exception:
                pass


def _write_for(item, excinfo):
    """Write the black box for `item`. With an exception, capture the full crash
    detail; otherwise (``--flight-all`` on a pass) a ring-only snapshot."""
    from ._capture import write_crash_flight
    from ._config import Config
    from ._install import _active, dump

    config = _active.config if _active is not None else Config()
    dest = Path(item.config._flight_dir) / f"{_safe_name(item.nodeid)}.flight"
    if excinfo is not None:
        exc_type, exc_value, exc_tb = excinfo
        return write_crash_flight(exc_type, exc_value, exc_tb, config, path=dest)
    return dump(dest, config=config)


# -- surface the path in the failure report ---------------------------------


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    if report.when != "call":
        return
    path = item.stash.get(_PATH_KEY, None)
    if path:
        report.sections.append(("Flight recording", f"black box: {path}"))
        report.user_properties.append(("flight", path))


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    written = getattr(config, "_flight_written", None)
    if not written:
        return
    tr = terminalreporter
    tr.write_sep("-", f"flight recorded {len(written)} black box(es)")
    for nodeid, path in written:
        tr.write_line(f"  {path}   ({nodeid})")
    tr.write_line("  inspect one with:  python -m flight inspect <path>")
