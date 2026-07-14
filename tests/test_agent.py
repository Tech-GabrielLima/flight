from __future__ import annotations

import difflib
import subprocess
import sys

import flight
from flight._agent import (
    CHANGES_BEHAVIOR,
    NO_PATCH,
    REJECTED,
    VERIFIED,
    AgentTools,
    apply_unified_diff,
    fix,
    heuristic_patch,
)


def compute_average(numbers):
    total = 0
    for n in numbers:
        total += n
    return total / len(numbers)


def lookup(d):
    return d["missing"]


def _crash_zero(path):
    flight.install()
    try:
        compute_average([])
    except ZeroDivisionError:
        flight.capture(path=str(path))
    finally:
        flight.uninstall()
    return str(path)


def _crash_key(path):
    flight.install()
    try:
        lookup({"a": 1})
    except KeyError:
        flight.capture(path=str(path))
    finally:
        flight.uninstall()
    return str(path)


def test_apply_diff_roundtrip():
    original = "a\nb\nc\nd\n"
    patched = "a\nB\nc\nd\n"
    diff = "".join(difflib.unified_diff(original.splitlines(True), patched.splitlines(True), "a/x", "b/x"))
    assert apply_unified_diff(original, diff) == patched


def test_apply_diff_insert():
    original = "def f():\n    return 1\n"
    patched = "def f():\n    x = 0\n    return 1\n"
    diff = "".join(difflib.unified_diff(original.splitlines(True), patched.splitlines(True), "a/x", "b/x"))
    assert apply_unified_diff(original, diff) == patched


def test_apply_diff_rejects_bad_context():
    original = "a\nb\nc\n"
    bad = "@@ -1,1 +1,1 @@\n zzz\n-a\n+A\n"
    assert apply_unified_diff(original, bad) is None


def test_agent_tools_are_queryable(tmp_path):
    tools = AgentTools(_crash_zero(tmp_path / "c.flight"))
    frames = tools.frames()
    assert frames and frames[0]["qualname"] == "compute_average"
    locs = tools.locals(0)
    assert any(l["name"] == "numbers" and l["length"] == 0 for l in locs)
    assert tools.exc_type == "ZeroDivisionError"
    assert "numbers" in tools.why(0, "numbers")


def test_fix_verifies_guard_on_canonical_crash(tmp_path):
    result = fix(_crash_zero(tmp_path / "c.flight"))
    assert result.status == VERIFIED
    assert result.verified
    assert "if not numbers" in result.patch
    assert "return 0.0" in result.patch
    assert "FIX VERIFIED" in result.report()


def test_heuristic_patch_is_a_valid_diff(tmp_path):
    tools = AgentTools(_crash_zero(tmp_path / "c.flight"))
    diff = heuristic_patch(tools)
    assert diff and diff.lstrip().startswith("---")
    patched = apply_unified_diff(tools.source(), diff)
    assert patched is not None and "if not numbers" in patched


def test_fix_no_patch_when_no_suspect(tmp_path):
    result = fix(_crash_key(tmp_path / "c.flight"))
    assert result.status == NO_PATCH


def test_fix_rejects_noop_patch_and_retries(tmp_path):
    path = _crash_zero(tmp_path / "c.flight")

    def noop_provider(tools, feedback=""):
        src = tools.source()
        lines = src.splitlines()
        patched = lines[:1] + ["# noop"] + lines[1:]
        return "".join(
            difflib.unified_diff(
                src.splitlines(True), ("\n".join(patched) + "\n").splitlines(True), "a/x", "b/x"
            )
        )

    result = fix(path, provider=noop_provider, max_tries=2)
    assert result.status == REJECTED
    assert result.tries == 2


def test_fix_flags_behaviour_change(tmp_path):
    path = _crash_zero(tmp_path / "c.flight")

    def raises_other(tools, feedback=""):
        src = tools.source()
        lines = src.splitlines()
        out = []
        for ln in lines:
            out.append(ln)
            if ln.startswith("def compute_average"):
                out.append("    raise KeyError('x')")
        patched = "\n".join(out) + "\n"
        return "".join(difflib.unified_diff(src.splitlines(True), patched.splitlines(True), "a/x", "b/x"))

    result = fix(path, provider=raises_other)
    assert result.status == CHANGES_BEHAVIOR
    assert "KeyError" in result.counterfactual


def test_fix_provider_returning_none(tmp_path):
    result = fix(_crash_zero(tmp_path / "c.flight"), provider=lambda tools, feedback="": None)
    assert result.status == NO_PATCH


def test_fix_no_crash_recording(tmp_path):
    flight.install()
    try:
        p = str(tmp_path / "snap.flight")
        flight.capture(path=p)
    finally:
        flight.uninstall()
    result = fix(p)
    assert result.status == NO_PATCH


def flaky_divide():
    import random

    n = random.randint(0, 100)
    numbers = [] if n >= 50 else [1, 2, 3]
    return sum(numbers) / len(numbers)


def test_fix_verifies_over_a_tape(tmp_path):
    p = str(tmp_path / "c.flight")
    for _ in range(300):
        try:
            with flight.deterministic(p):
                flaky_divide()
        except ZeroDivisionError:
            break
    else:
        raise AssertionError("never crashed")
    result = fix(p)
    assert result.status in (VERIFIED, CHANGES_BEHAVIOR)


def test_cli_fix(tmp_path):
    path = _crash_zero(tmp_path / "c.flight")
    out = tmp_path / "fix.patch"
    proc = subprocess.run(
        [sys.executable, "-m", "flight", "fix", path, "-o", str(out)],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert "FIX VERIFIED" in proc.stdout
    assert out.exists() and "if not numbers" in out.read_text()
