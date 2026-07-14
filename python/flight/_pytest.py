from __future__ import annotations

import re
from pathlib import Path

import pytest

_PATH_KEY = pytest.StashKey[str]()

_SANITIZE = re.compile(r"[^A-Za-z0-9_.-]+")


def _safe_name(nodeid: str, max_len: int = 120) -> str:
    name = _SANITIZE.sub("_", nodeid).strip("_")
    if len(name) > max_len:
        name = name[-max_len:].lstrip("_")
    return name or "test"


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
    config._flight_written = []
    if config._flight_enabled:
        out = Path(config.getoption("--flight-dir"))
        try:
            out.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        config._flight_dir = out


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item):
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
    from ._capture import write_crash_flight
    from ._config import Config
    from ._install import _active, dump

    config = _active.config if _active is not None else Config()
    dest = Path(item.config._flight_dir) / f"{_safe_name(item.nodeid)}.flight"
    if excinfo is not None:
        exc_type, exc_value, exc_tb = excinfo
        return write_crash_flight(exc_type, exc_value, exc_tb, config, path=dest)
    return dump(dest, config=config)


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
