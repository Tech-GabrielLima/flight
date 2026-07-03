"""Phase 7 — the intelligence layer: explain, fingerprint, repro --pytest, queries."""

from __future__ import annotations

import subprocess
import sys

import flight
from flight._explain import analyze, build_context, explain, prompt_text
from flight._fingerprint import fingerprint, signature


def compute_average(numbers):  # module-level so `repro` can resolve it (no <locals>)
    total = 0
    for n in numbers:
        total += n
    return total / len(numbers)


def _crash_zero(path):
    """Record a ZeroDivisionError crash with an empty-list culprit."""
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

    def lookup(d):
        return d["missing"]

    try:
        lookup({"a": 1})
    except KeyError:
        flight.capture(path=str(path))
    finally:
        flight.uninstall()
    return str(path)


# -- explain ----------------------------------------------------------------


def test_explain_heuristic_identifies_root_cause(tmp_path):
    path = _crash_zero(tmp_path / "c.flight")
    ex = explain(path)
    assert "ZeroDivisionError" in ex.summary
    assert "compute_average" in ex.summary
    assert any("empty" in s for s in ex.suspects)
    assert "divisor is zero" in ex.summary
    assert ex.llm is None  # offline by default


def test_explain_prompt_bundles_context(tmp_path):
    path = _crash_zero(tmp_path / "c.flight")
    ctx = build_context(path)
    prompt = prompt_text(ctx)
    assert "Exception chain" in prompt
    assert "compute_average" in prompt
    assert "Source at the crash" in prompt
    assert "root cause" in prompt.lower()


def test_explain_uses_injected_provider(tmp_path):
    path = _crash_zero(tmp_path / "c.flight")
    seen = {}

    def fake_llm(prompt):
        seen["prompt"] = prompt
        return "The list was empty."

    ex = explain(path, provider=fake_llm)
    assert ex.llm == "The list was empty."
    assert "ZeroDivisionError" in seen["prompt"]
    assert "model explanation" in ex.render()


def test_explain_provider_failure_is_contained(tmp_path):
    path = _crash_zero(tmp_path / "c.flight")

    def broken(_prompt):
        raise RuntimeError("no network")

    ex = explain(path, provider=broken)
    assert "model call failed" in ex.llm  # never propagates (P1)


def test_explain_no_crash(tmp_path):
    # a scope recording (no crash) explains gracefully
    with flight.record(path=str(tmp_path / "s.flight")):
        x = 1  # noqa: F841
    ex = explain(str(tmp_path / "s.flight"))
    assert "no crash" in ex.summary.lower()


# -- fingerprint / dedup ----------------------------------------------------


def test_same_bug_same_fingerprint(tmp_path):
    a = _crash_zero(tmp_path / "a.flight")
    b = _crash_zero(tmp_path / "b.flight")
    assert fingerprint(a) == fingerprint(b)


def test_different_bugs_differ(tmp_path):
    a = _crash_zero(tmp_path / "a.flight")
    b = _crash_key(tmp_path / "b.flight")
    assert fingerprint(a) != fingerprint(b)


def test_signature_components(tmp_path):
    sig = signature(_crash_zero(tmp_path / "a.flight"))
    assert "ZeroDivisionError" in sig.exceptions
    assert any(q.endswith("compute_average") for q, _f, _o in sig.frames)


def test_fingerprint_cli(tmp_path):
    path = _crash_zero(tmp_path / "c.flight")
    out = subprocess.run(
        [sys.executable, "-m", "flight", "fingerprint", path], capture_output=True, text=True
    )
    assert out.returncode == 0
    assert len(out.stdout.strip()) == 16  # blake2b digest_size=8 -> 16 hex chars


# -- repro --pytest ---------------------------------------------------------


def test_repro_pytest_emits_a_passing_regression_test(tmp_path):
    path = _crash_zero(tmp_path / "c.flight")
    test_file = tmp_path / "test_repro.py"
    result = flight.repro(path, str(test_file))  # standalone first: sanity
    assert result.verified is True

    from flight._repro import write_repro

    res = write_repro(path, str(test_file), verify=True, pytest=True)
    assert res.verified is True
    text = test_file.read_text()
    assert "def test_regression" in text
    assert "pytest.raises" in text
    # and it actually passes under pytest
    run = subprocess.run(
        [sys.executable, "-m", "pytest", str(test_file), "-q"], capture_output=True, text=True
    )
    assert run.returncode == 0, run.stdout + run.stderr


# -- semantic timeline queries ----------------------------------------------


def test_semantic_size_query(tmp_path):
    path = str(tmp_path / "cache.flight")
    with flight.record(path=path) as rec:
        cache = {}
        rec.watch(cache, name="cache")
        for i in range(150):
            cache[i] = i * i

    tt = flight.time_travel(path)
    step = tt.find_first("len(cache) > 100")
    assert step is not None
    assert step.kind == "item"
    assert step.name == "cache"
    # the 101st distinct key (i == 100) is the write that crossed 100 entries
    assert step.key == "100"


def test_semantic_size_query_never_reaches(tmp_path):
    path = str(tmp_path / "small.flight")
    with flight.record(path=path) as rec:
        cache = {}
        rec.watch(cache, name="cache")
        for i in range(5):
            cache[i] = i

    tt = flight.time_travel(path)
    assert tt.find_first("len(cache) > 100") is None
