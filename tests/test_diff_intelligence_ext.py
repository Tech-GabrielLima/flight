"""Extended suite for the phase 6/7 tooling: delta debugging (`_ddmin`),
comparison (`_diff`), fingerprinting (`_fingerprint`), the intelligence layer
(`_explain`) and reproduction (`_repro`).

Emphasis is on *properties* — ddmin 1-minimality, fingerprint
stability/discrimination — and on the contained-failure paths (a hostile
provider never breaks `explain`, an unresolvable `<locals>` repro never
verifies). Pure functions are exercised with heavy parametrization; the
file-level tools are exercised against real recorded `.flight` files.

Nothing here calls a real LLM: only injected fake providers are used, and no
test requires ANTHROPIC_API_KEY.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from itertools import combinations

import pytest

import flight
from flight import Mutation, Recording
from flight._ddmin import (
    MinimizeResult,
    _NEUTRAL,
    _neutralize,
    ddmin,
    minimize_tape,
)
from flight._diff import (
    Divergence,
    _target,
    diff_events,
    diff_files,
    diff_mutations,
    diff_tapes,
)
from flight._explain import analyze, build_context, explain, prompt_text
from flight._fingerprint import fingerprint, signature
from flight._nondet import Tape
from flight._repro import build_repro, write_repro


# ===========================================================================
# module-level crash functions (repro needs a resolvable, non-<locals> qualname)
# ===========================================================================


def _avg(nums):
    total = 0
    for n in nums:
        total += n
    return total / len(nums)  # ZeroDivisionError on []


def _index(xs):
    return xs[5]  # IndexError on a short/empty list


def _key(d):
    return d["missing"]  # KeyError


def _attr(x):
    return x.foo  # AttributeError when x is None


class _Widget:
    def __init__(self):
        self.size = 3


def _use_widget(w):
    return w.parts[w.size]  # AttributeError: no 'parts' -> stub reconstruction


def _make_closure():
    def _inner(x):
        return x[5]  # IndexError inside a <locals> closure

    return _inner


# -- recording helpers ------------------------------------------------------


def _crash(tmp_path, fn, arg, exc, name):
    """Record an in-process crash of ``fn(arg)`` to a `.flight` and return path."""
    path = str(tmp_path / name)
    flight.install()
    try:
        fn(arg)
    except exc:
        flight.capture(path=path)
    finally:
        flight.uninstall()
    return path


def _record_crash_script(tmp_path, source, tag):
    """Run `source` under `flight run` in its own dir (so basename == prog.py)."""
    d = tmp_path / tag
    d.mkdir()
    script = d / "prog.py"
    script.write_text(textwrap.dedent(source))
    out = d / "out"
    out.mkdir()
    subprocess.run(
        [sys.executable, "-m", "flight", "run", "--output-dir", str(out), str(script)],
        capture_output=True,
        text=True,
    )
    files = list(out.glob("*.flight"))
    assert files, "no crash file produced"
    return str(files[0])


def _mut(seq, name, repr_, *, kind="local", key=None, line=10):
    return Mutation(
        seq=seq,
        kind=kind,
        name=name,
        key=key,
        value=("int", repr_, None, None),
        file="t.py",
        qualname="f",
        line=line,
        frame=1,
    )


def _rec(specs):
    """Build a Recording from ``[(name, repr), ...]`` local writes."""
    return Recording([_mut(i, n, r) for i, (n, r) in enumerate(specs)])


def _is_1_minimal(items, test, result):
    """True iff `result` is interesting and removing any single element is not."""
    if not test(result):
        return False
    for e in result:
        reduced = [x for x in result if x != e]
        if reduced and test(reduced):
            return False
    return True


# ===========================================================================
# ddmin — the pure, generic delta-debugging core
# ===========================================================================


@pytest.mark.parametrize("culprit", list(range(15)))
def test_ddmin_single_culprit_is_isolated(culprit):
    items = list(range(15))
    result = ddmin(items, lambda s: culprit in s)
    assert result == [culprit]


@pytest.mark.parametrize("i,j", list(combinations(range(9), 2))[:24])
def test_ddmin_pair_culprit_is_exactly_the_pair(i, j):
    items = list(range(9))
    result = ddmin(items, lambda s: i in s and j in s)
    assert set(result) == {i, j}
    assert _is_1_minimal(items, lambda s: i in s and j in s, result)


@pytest.mark.parametrize(
    "m,k",
    [(5, 3), (6, 2), (4, 4), (8, 5), (3, 1), (7, 6), (5, 1), (6, 6), (9, 4), (4, 2)],
)
def test_ddmin_needs_any_k_of_required(m, k):
    required = set(range(m))
    items = list(range(m + 4))
    test = lambda s: len(required & set(s)) >= k  # noqa: E731
    result = ddmin(items, test)
    assert len(result) == k
    assert set(result) <= required
    assert _is_1_minimal(items, test, result)


@pytest.mark.parametrize("n", [2, 3, 4, 8, 16, 33])
def test_ddmin_all_interesting_shrinks_to_one(n):
    items = list(range(n))
    result = ddmin(items, lambda s: True)
    assert len(result) == 1
    assert result[0] in items


@pytest.mark.parametrize("n", [1, 2, 5, 10, 20])
def test_ddmin_nothing_interesting_returns_input_unchanged(n):
    # ddmin assumes test(full) is true; when it is not, it cannot reduce and
    # returns its input as-is. Documenting that boundary behavior.
    items = list(range(n))
    assert ddmin(items, lambda s: False) == items


@pytest.mark.parametrize("n", [1, 2, 3, 7])
def test_ddmin_singleton_and_tiny_inputs(n):
    items = list(range(n))
    # "needs the last element" -> minimal is just that element.
    result = ddmin(items, lambda s: (n - 1) in s)
    assert result == [n - 1]


# 1-minimality property over a spread of monotone required-set predicates.
def _required_predicates():
    cases = []
    import random as _r

    rng = _r.Random(20260703)
    for _ in range(40):
        n = rng.randint(4, 14)
        items = list(range(n))
        size = rng.randint(1, min(4, n))
        required = frozenset(rng.sample(items, size))
        cases.append((items, required))
    return cases


@pytest.mark.parametrize("items,required", _required_predicates())
def test_ddmin_required_set_is_recovered_exactly(items, required):
    test = lambda s: required <= set(s)  # noqa: E731
    result = ddmin(items, test)
    assert set(result) == set(required)
    assert _is_1_minimal(items, test, result)


@pytest.mark.parametrize(
    "pred",
    [
        lambda s: 0 in s or 5 in s,
        lambda s: 3 in s,
        lambda s: len([x for x in s if x % 2 == 0]) >= 2,
        lambda s: 1 in s and 8 in s,
        lambda s: max(s) if s else False,
        lambda s: 2 in s or (4 in s and 6 in s),
        lambda s: len(s) >= 1,
    ],
)
def test_ddmin_is_always_1_minimal(pred):
    items = list(range(10))
    assert pred(items)  # precondition: the full input is interesting
    result = ddmin(items, pred)
    assert _is_1_minimal(items, pred, result)


# ===========================================================================
# _neutralize / _NEUTRAL / MinimizeResult
# ===========================================================================


@pytest.mark.parametrize("tag,neutral", sorted(_NEUTRAL.items()))
def test_neutralize_replaces_payload_with_the_neutral_default(tag, neutral):
    row = (7, "random.randint", tag, "original-payload")
    seq, src, t, payload = _neutralize(row)
    assert (seq, src, t) == (7, "random.randint", tag)  # identity preserved
    assert payload == neutral  # only the payload is defaulted


def test_neutral_covers_the_expected_tags():
    assert set(_NEUTRAL) == {"i", "f", "o", "s", "b"}


@pytest.mark.parametrize("reproduced", [False])
def test_minimize_result_render_when_not_reproduced(reproduced):
    r = MinimizeResult(reproduced=reproduced, total=3, kept=[0, 1, 2])
    assert "did not reproduce" in r.render()


def test_minimize_result_render_lists_kept_rows():
    r = MinimizeResult(
        reproduced=True,
        total=5,
        kept=[3],
        kept_rows=[(3, "random.randint", "i", "95")],
        neutralized=4,
    )
    text = r.render()
    assert "minimal reproducer" in text
    assert "1 of 5" in text
    assert "95" in text and "random.randint" in text


# -- minimize_tape wired to the real replay engine --------------------------


def _crashy_at(k):
    """A function that crashes iff the k-th of six recorded draws exceeds 90."""
    import random

    def fn():
        vals = [random.randint(0, 100) for _ in range(6)]
        if vals[k] > 90:
            raise ValueError("boom")
        return vals

    return fn


@pytest.mark.parametrize("k", list(range(6)))
def test_minimize_isolates_the_single_load_bearing_draw(k):
    draws = [10, 20, 30, 40, 50, 60]
    draws[k] = 95  # only this one pushes past the threshold
    rows = [(i, "random.randint", "i", str(v)) for i, v in enumerate(draws)]
    res = minimize_tape(Tape(rows), _crashy_at(k))
    assert res.reproduced
    assert res.total == 6
    assert res.kept == [k]
    assert res.neutralized == 5
    assert res.kept_rows[0][3] == "95"


@pytest.mark.parametrize("k", [0, 3, 5])
def test_minimize_reports_no_reproduction_when_nothing_crosses_threshold(k):
    rows = [(i, "random.randint", "i", str(v)) for i, v in enumerate([1, 2, 3, 4, 5, 6])]
    res = minimize_tape(Tape(rows), _crashy_at(k))
    assert res.reproduced is False


# ===========================================================================
# _diff — mutation timelines
# ===========================================================================


@pytest.mark.parametrize("length", [0, 1, 2, 5, 10])
def test_diff_mutations_identical(length):
    specs = [(f"v{i}", str(i)) for i in range(length)]
    a, b = _rec(specs), _rec(specs)
    d = diff_mutations(a, b)
    assert d.kind == "mutation"
    assert d.identical
    assert not d  # __bool__ is False when identical
    assert d.index is None
    assert d.compared == length
    assert "identical" in d.render()


@pytest.mark.parametrize("k", list(range(8)))
def test_diff_mutations_first_differing_value(k):
    specs = [(f"v{i}", str(i)) for i in range(8)]
    other = list(specs)
    other[k] = (other[k][0], "999")  # same target, different value at index k
    d = diff_mutations(_rec(specs), _rec(other))
    assert not d.identical
    assert bool(d) is True
    assert d.index == k
    assert f"v{k}" in d.detail
    assert d.left != d.right
    assert "diverged at step" in d.render()


def test_diff_mutations_target_change_by_name():
    a = _rec([("a", "1"), ("b", "2"), ("c", "3")])
    b = _rec([("a", "1"), ("X", "2"), ("c", "3")])
    d = diff_mutations(a, b)
    assert d.index == 1
    assert "vs" in d.detail  # different write here (b vs X)


@pytest.mark.parametrize(
    "mutator,idx",
    [
        (lambda m: {"kind": "item", "key": "0"}, 1),  # local -> item write
        (lambda m: {"kind": "attr", "key": "x"}, 1),  # local -> attr write
        (lambda m: {"line": 99}, 1),  # same target, different source line
    ],
)
def test_diff_mutations_target_key_divergence(mutator, idx):
    # `_mut_key` = (kind, name, key, line), so any of these changes the write's
    # identity → divergence at index 1. (A differing variable *name* is covered
    # by test_diff_mutations_target_change_by_name, which can pass `name` cleanly.)
    base = [_mut(0, "a", "1"), _mut(1, "b", "2"), _mut(2, "c", "3")]
    changed = [_mut(0, "a", "1"), _mut(1, "b", "2", **mutator(None)), _mut(2, "c", "3")]
    d = diff_mutations(Recording(base), Recording(changed))
    assert not d.identical
    assert d.index == idx


@pytest.mark.parametrize("la,lb", [(3, 2), (2, 3), (5, 1), (1, 5), (4, 0), (0, 4)])
def test_diff_mutations_length_mismatch(la, lb):
    a = _rec([(f"v{i}", str(i)) for i in range(la)])
    b = _rec([(f"v{i}", str(i)) for i in range(lb)])
    d = diff_mutations(a, b)
    assert not d.identical
    assert d.index == min(la, lb)
    longer = "left" if la > lb else "right"
    assert longer in d.detail
    assert "kept writing" in d.detail


@pytest.mark.parametrize(
    "kind,key,expected",
    [
        ("local", None, "x"),
        ("item", "0", "x[0]"),
        ("attr", "field", "x.field"),
    ],
)
def test_target_rendering(kind, key, expected):
    m = _mut(0, "x", "1", kind=kind, key=key)
    assert _target(m) == expected


# ===========================================================================
# _diff — non-determinism tapes
# ===========================================================================


@pytest.mark.parametrize("n", [0, 1, 3, 6])
def test_diff_tapes_identical(n):
    rows = [(i, "random.random", "f", str(i / 10)) for i in range(n)]
    d = diff_tapes(Tape(rows), Tape(list(rows)))
    assert d.kind == "nondet"
    assert d.identical
    assert d.compared == n


@pytest.mark.parametrize("k", list(range(5)))
def test_diff_tapes_control_flow_branch_at_k(k):
    rows_a = [(i, "random.random", "f", "0.5") for i in range(5)]
    rows_b = list(rows_a)
    rows_b[k] = (k, "os.urandom", "b", "0.5")  # different source == branch
    d = diff_tapes(Tape(rows_a), Tape(rows_b))
    assert not d.identical
    assert d.index == k
    assert "branched" in d.detail


@pytest.mark.parametrize("k", list(range(5)))
def test_diff_tapes_answer_mismatch_at_k(k):
    rows_a = [(i, "random.random", "f", "0.5") for i in range(5)]
    rows_b = list(rows_a)
    rows_b[k] = (k, "random.random", "f", "0.9")  # same source, different payload
    d = diff_tapes(Tape(rows_a), Tape(rows_b))
    assert not d.identical
    assert d.index == k
    assert "answered differently" in d.detail


@pytest.mark.parametrize("la,lb", [(3, 2), (2, 4), (5, 1)])
def test_diff_tapes_length_mismatch(la, lb):
    rows_a = [(i, "random.random", "f", "0.5") for i in range(la)]
    rows_b = [(i, "random.random", "f", "0.5") for i in range(lb)]
    d = diff_tapes(Tape(rows_a), Tape(rows_b))
    assert not d.identical
    assert d.index == min(la, lb)
    assert "more boundary calls" in d.detail


# ===========================================================================
# _diff — event rings
# ===========================================================================


def _ev(kind, line, qual="f", file="x.py"):
    return (kind, file, qual, line)


@pytest.mark.parametrize("n", [0, 1, 4, 9])
def test_diff_events_identical(n):
    evs = [_ev("LINE", i) for i in range(n)]
    d = diff_events(evs, list(evs))
    assert d.kind == "event"
    assert d.identical


@pytest.mark.parametrize("k", list(range(6)))
def test_diff_events_diverge_at_k(k):
    a = [_ev("LINE", i) for i in range(6)]
    b = list(a)
    b[k] = _ev("LINE", 100 + k)  # a different line at position k
    d = diff_events(a, b)
    assert not d.identical
    assert d.index == k
    assert "diverged" in d.detail


@pytest.mark.parametrize("la,lb", [(3, 2), (2, 5)])
def test_diff_events_length_mismatch(la, lb):
    a = [_ev("LINE", i) for i in range(la)]
    b = [_ev("LINE", i) for i in range(lb)]
    d = diff_events(a, b)
    assert not d.identical
    assert d.index == min(la, lb)
    assert ("left" in d.detail) or ("right" in d.detail)
    assert "ran longer" in d.detail


# ===========================================================================
# Divergence dataclass semantics + CLI exit-code alignment
# ===========================================================================


def test_divergence_bool_and_render_identical():
    d = Divergence("mutation", True, None, None, None, "the recordings match", 5)
    assert not d
    assert bool(d) is False
    assert "identical (5 steps" in d.render()


def test_divergence_bool_and_render_diverged():
    d = Divergence("nondet", False, 2, "L", "R", "answered differently", 4)
    assert d
    assert bool(d) is True
    text = d.render()
    assert "diverged at step 2" in text
    assert "left : L" in text and "right: R" in text


def test_divergence_incomparable_is_truthy():
    d = Divergence("incomparable", False, None, None, None, "no shared axis", 0)
    assert d  # incomparable counts as "not identical"
    assert d.kind == "incomparable"


# ===========================================================================
# diff_files — auto-detecting axis over real .flight files
# ===========================================================================


def _scope_file(path, extra):
    with flight.record(path=str(path)):
        total = 0
        for i in range(4):
            total = total + i + extra  # noqa: F841
    return str(path)


def _tape_file(path, seed):
    import random

    def work():
        random.seed(seed)
        return [random.randint(1, 6) for _ in range(5)]

    with flight.deterministic(str(path)):
        work()
    return str(path)


def test_diff_files_mutation_axis_identical(tmp_path):
    # A byte-for-byte copy is the only way to get two *identical* scope files:
    # recording twice captures the frame's own locals (e.g. `path`), which
    # differ per run — so two fresh recordings legitimately diverge at step 0.
    import shutil

    a = _scope_file(tmp_path / "a.flight", extra=0)
    b = str(tmp_path / "b.flight")
    shutil.copy(a, b)
    d = diff_files(a, b)
    assert d.kind == "mutation"
    assert d.identical


def test_diff_files_mutation_axis_diverges(tmp_path):
    a = _scope_file(tmp_path / "a.flight", extra=0)
    b = _scope_file(tmp_path / "b.flight", extra=10)
    d = diff_files(a, b)
    assert d.kind == "mutation"
    assert not d.identical
    assert d.index is not None


def test_diff_files_nondet_axis_identical(tmp_path):
    a = _tape_file(tmp_path / "a.flight", seed=1)
    b = _tape_file(tmp_path / "b.flight", seed=1)
    d = diff_files(a, b)
    assert d.kind == "nondet"
    assert d.identical


def test_diff_files_nondet_axis_diverges(tmp_path):
    a = _tape_file(tmp_path / "a.flight", seed=1)
    b = _tape_file(tmp_path / "b.flight", seed=2)
    d = diff_files(a, b)
    assert d.kind == "nondet"
    assert not d.identical
    assert "random" in d.detail


def test_diff_files_a_file_against_itself_is_identical(tmp_path):
    a = _tape_file(tmp_path / "a.flight", seed=5)
    assert diff_files(a, a).identical


def test_diff_files_event_axis_identical_copy(tmp_path):
    import shutil

    a = str(tmp_path / "ring.flight")
    flight.install()
    (lambda: sum(range(5)))()
    flight.dump(a)
    flight.uninstall()
    copy = str(tmp_path / "ring_copy.flight")
    shutil.copy(a, copy)
    d = diff_files(a, copy)
    assert d.identical  # byte-identical ring dumps never diverge


# ===========================================================================
# _fingerprint
# ===========================================================================


def test_fingerprint_is_16_hex_chars(tmp_path):
    p = _crash(tmp_path, _avg, [], ZeroDivisionError, "c.flight")
    fp = fingerprint(p)
    assert len(fp) == 16
    assert all(c in "0123456789abcdef" for c in fp)


@pytest.mark.parametrize("run", range(6))
def test_fingerprint_is_stable_across_runs(tmp_path, run):
    p = _crash(tmp_path, _avg, [], ZeroDivisionError, f"c{run}.flight")
    # recomputing over the same file is deterministic
    assert fingerprint(p) == fingerprint(p)


def test_same_bug_recorded_twice_shares_a_fingerprint(tmp_path):
    a = _crash(tmp_path, _avg, [], ZeroDivisionError, "a.flight")
    b = _crash(tmp_path, _avg, [], ZeroDivisionError, "b.flight")
    assert fingerprint(a) == fingerprint(b)


def test_fingerprint_stable_under_edits_off_the_crash_path(tmp_path):
    # The fingerprint keys off each frame's in-function offset, so edits that
    # don't move the lines on the path to the crash leave it unchanged. Here the
    # crash function and its call site are byte-identical; only unrelated code
    # *after* the crash differs — so every executed frame's offset is preserved.
    base = "def crash(xs):\n    return xs[5]\ncrash([1, 2])\n"
    a = _record_crash_script(tmp_path, base, "plain")
    b = _record_crash_script(tmp_path, base + "\n\ndef unused():\n    return 42\n", "edited")
    assert fingerprint(a) == fingerprint(b)


def test_fingerprint_shifts_when_the_module_call_line_moves(tmp_path):
    # Honest limitation: padding *before* the crash moves the <module> frame's
    # call line relative to the module's start, so its offset — and thus the
    # fingerprint — changes. Stability holds within functions, not for top-level
    # line position. Documented as a real property, not asserted as equality.
    src = "{pad}def crash(xs):\n    return xs[5]\ncrash([1, 2])\n"
    a = _record_crash_script(tmp_path, src.format(pad=""), "near")
    b = _record_crash_script(tmp_path, src.format(pad="\n\n\n\n\n"), "far")
    assert fingerprint(a) != fingerprint(b)


@pytest.mark.parametrize(
    "fn_a,arg_a,exc_a,fn_b,arg_b,exc_b",
    [
        (_avg, [], ZeroDivisionError, _key, {}, KeyError),
        (_avg, [], ZeroDivisionError, _index, [], IndexError),
        (_key, {}, KeyError, _attr, None, AttributeError),
        (_index, [], IndexError, _attr, None, AttributeError),
    ],
)
def test_different_bugs_get_different_fingerprints(
    tmp_path, fn_a, arg_a, exc_a, fn_b, arg_b, exc_b
):
    a = _crash(tmp_path, fn_a, arg_a, exc_a, "a.flight")
    b = _crash(tmp_path, fn_b, arg_b, exc_b, "b.flight")
    assert fingerprint(a) != fingerprint(b)


def test_signature_components(tmp_path):
    sig = signature(_crash(tmp_path, _avg, [], ZeroDivisionError, "a.flight"))
    assert "ZeroDivisionError" in sig.exceptions
    assert any(q.endswith("_avg") for q, _f, _o in sig.frames)
    assert isinstance(sig.state_kinds, list)
    assert sig.state_kinds == sorted(sig.state_kinds)  # kinds are sorted/stable


def test_signature_of_ring_only_file_is_empty(tmp_path):
    p = str(tmp_path / "ring.flight")
    flight.install()
    (lambda: sum(range(3)))()
    flight.dump(p)
    flight.uninstall()
    sig = signature(p)
    assert sig.exceptions == [] and sig.frames == [] and sig.state_kinds == []


# ===========================================================================
# _explain
# ===========================================================================


@pytest.mark.parametrize(
    "fn,arg,exc,exc_name,suspect_sub,pointed",
    [
        (_avg, [], ZeroDivisionError, "ZeroDivisionError", "empty", "divisor is zero"),
        (_index, [], IndexError, "IndexError", "empty", "empty/short"),
        (_key, {}, KeyError, "KeyError", "empty", "empty/short"),
        (_attr, None, AttributeError, "AttributeError", "is None", "attribute was accessed on None"),
    ],
)
def test_explain_heuristic_root_cause(tmp_path, fn, arg, exc, exc_name, suspect_sub, pointed):
    p = _crash(tmp_path, fn, arg, exc, "c.flight")
    ex = explain(p)
    assert exc_name in ex.summary
    assert fn.__name__ in ex.summary  # names the crash frame
    assert any(suspect_sub in s for s in ex.suspects)
    assert pointed in ex.summary
    assert ex.llm is None  # offline by default, no provider


@pytest.mark.parametrize(
    "fn,arg,exc,exc_name",
    [
        (_avg, [], ZeroDivisionError, "ZeroDivisionError"),
        (_index, [], IndexError, "IndexError"),
        (_key, {}, KeyError, "KeyError"),
        (_attr, None, AttributeError, "AttributeError"),
    ],
)
def test_explain_prompt_bundles_full_context(tmp_path, fn, arg, exc, exc_name):
    p = _crash(tmp_path, fn, arg, exc, "c.flight")
    ctx = build_context(p)
    prompt = prompt_text(ctx)
    assert "Exception chain" in prompt
    assert exc_name in prompt
    assert "Stack (crash first)" in prompt
    assert "Source at the crash" in prompt
    assert "Locals in the crash frame" in prompt
    assert "root cause" in prompt.lower()
    assert fn.__name__ in prompt


def test_build_context_marks_has_crash(tmp_path):
    p = _crash(tmp_path, _avg, [], ZeroDivisionError, "c.flight")
    ctx = build_context(p)
    assert ctx["has_crash"] is True
    assert ctx["exceptions"]
    assert ctx["crash_frame"]["qualname"].endswith("_avg")
    assert ctx["source_window"]  # source captured -> a window exists


def test_explain_uses_injected_provider(tmp_path):
    p = _crash(tmp_path, _avg, [], ZeroDivisionError, "c.flight")
    seen = {}

    def fake(prompt):
        seen["prompt"] = prompt
        return "The list was empty."

    ex = explain(p, provider=fake)
    assert ex.llm == "The list was empty."
    assert "ZeroDivisionError" in seen["prompt"]
    assert "model explanation" in ex.render()


@pytest.mark.parametrize(
    "boom",
    [
        RuntimeError("no network"),
        ValueError("bad key"),
        KeyError("nope"),
        TimeoutError("slow"),
    ],
)
def test_explain_provider_failure_is_contained(tmp_path, boom):
    p = _crash(tmp_path, _avg, [], ZeroDivisionError, "c.flight")

    def broken(_prompt):
        raise boom

    ex = explain(p, provider=broken)  # must not raise (P1)
    assert "model call failed" in ex.llm
    assert type(boom).__name__ in ex.llm


def test_explain_ring_only_file_says_no_crash(tmp_path):
    p = str(tmp_path / "s.flight")
    with flight.record(path=p):
        x = 1  # noqa: F841
    ex = explain(p)
    assert "no crash" in ex.summary.lower()
    assert ex.suspects == []
    assert "nothing to explain" in ex.prompt.lower()


def test_analyze_no_crash_context_directly():
    summary, suspects = analyze({"has_crash": False})
    assert "no crash" in summary.lower()
    assert suspects == []


def test_analyze_crash_without_exceptions():
    summary, suspects = analyze({"has_crash": True, "exceptions": []})
    assert "not captured" in summary
    assert suspects == []


# ===========================================================================
# _repro
# ===========================================================================


def test_repro_verifies_a_zero_division(tmp_path):
    p = _crash(tmp_path, _avg, [], ZeroDivisionError, "c.flight")
    res = write_repro(p, tmp_path / "repro.py", verify=True)
    assert res.verified is True
    assert res.path.exists()
    assert "_avg" in res.script


@pytest.mark.parametrize(
    "fn,arg,exc",
    [
        (_avg, [], ZeroDivisionError),
        (_index, [], IndexError),
        (_key, {}, KeyError),
    ],
)
def test_repro_verifies_across_scalar_container_crashes(tmp_path, fn, arg, exc):
    p = _crash(tmp_path, fn, arg, exc, "c.flight")
    res = write_repro(p, tmp_path / "repro.py", verify=True)
    assert res.verified is True
    # run it once more, fresh, to confirm it is self-contained
    proc = subprocess.run(
        [sys.executable, str(res.path)], capture_output=True, text=True
    )
    assert "FLIGHT_REPRO_OK" in proc.stdout


def test_repro_pytest_emits_a_passing_regression_test(tmp_path):
    p = _crash(tmp_path, _avg, [], ZeroDivisionError, "c.flight")
    test_file = tmp_path / "test_repro_generated.py"
    res = write_repro(p, str(test_file), verify=True, pytest=True)
    assert res.verified is True
    text = test_file.read_text()
    assert "def test_regression" in text
    assert "pytest.raises" in text
    run = subprocess.run(
        [sys.executable, "-m", "pytest", str(test_file), "-q"],
        capture_output=True,
        text=True,
    )
    assert run.returncode == 0, run.stdout + run.stderr


def test_repro_opaque_object_is_stub_and_approximate(tmp_path):
    p = _crash(tmp_path, _use_widget, _Widget(), AttributeError, "c.flight")
    res = build_repro(p)
    assert res.script
    assert "_Stub(" in res.script
    assert res.approximate is True
    assert any("stub" in n for n in res.notes)


def test_repro_nested_locals_function_is_not_verified(tmp_path):
    # A crash inside a <locals> closure cannot be resolved by import; generation
    # still succeeds (best-effort) but verification must fail.
    path = str(tmp_path / "c.flight")
    flight.install()
    try:
        _make_closure()([1, 2])
    except IndexError:
        flight.capture(path=path)
    finally:
        flight.uninstall()
    res = write_repro(path, tmp_path / "repro.py", verify=True)
    assert "<locals>" in res.script
    assert res.verified is not True  # unresolved qualname -> cannot verify


def test_repro_reports_no_crash_file(tmp_path):
    p = str(tmp_path / "ring.flight")
    flight.install()
    (lambda: sum(range(5)))()
    flight.dump(p)
    flight.uninstall()
    res = build_repro(p)
    assert res.script == ""
    assert "no crash" in res.reason
