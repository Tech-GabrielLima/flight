"""Coverage cleanup: the last in-process line the module suites didn't reach."""

from __future__ import annotations

from flight._timetravel import LineBreakpoint, Step


def _step(line, file="app.py"):
    return Step(
        index=0, seq=1, kind="local", name="x", key=None, file=file,
        qualname="f", line=line, frame=1, value_repr="1", raw=("int", "1", None, None),
    )


def test_line_breakpoint_without_a_file_matches_any_file_on_that_line():
    # An empty `file` means "any file" — the `return True` branch (line 181).
    bp = LineBreakpoint(file="", line=5)
    assert bp.matches(_step(5, file="whatever.py")) is True
    assert bp.matches(_step(6, file="whatever.py")) is False


def test_line_breakpoint_with_a_file_matches_by_basename_and_suffix():
    bp = LineBreakpoint(file="app.py", line=5)
    assert bp.matches(_step(5, file="/abs/path/app.py")) is True
    assert bp.matches(_step(5, file="other.py")) is False
