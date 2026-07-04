"""Extended, heavily-parametrized suite for Phase-2 scope recording, the Phase-5
time-travel engine, and the DAP adapter.

Covers: `with flight.record()` / `watch()` capture semantics (locals, list/dict/
object-attr, deletions, scrubbing, mutation cap, nested scopes, opt-in), the
:class:`TimeTravel` cursor (bounds, state reconstruction at every position, the
query engine `find_first/find_all/find_last` + `parse_condition`/`parse_len`/
`coerce_value` on valid AND malformed input), and :class:`DebugAdapter.handle`
for every command plus the Content-Length framing / `serve` transport.

Recording objects for the pure engine tests are synthesized from
:class:`flight._read.Mutation` so the timeline is deterministic and independent
of CPython line-event timing.
"""

from __future__ import annotations

import io
import json

import pytest

import flight
from flight import Config
from flight._dap import DebugAdapter, read_message, serve, write_message
from flight._read import Mutation, Recording
from flight._timetravel import (
    LineBreakpoint,
    Step,
    TimeTravel,
    Watchpoint,
    coerce_value,
    parse_condition,
    parse_len,
)


# ===========================================================================
# synthetic-recording helpers (deterministic timelines for the engine tests)
# ===========================================================================


def _value_tuple(val):
    if isinstance(val, tuple):
        return val
    if val is None:
        return ("none", "None", None, None)
    if isinstance(val, bool):
        return ("bool", str(val), None, None)
    if isinstance(val, int):
        return ("int", str(val), None, None)
    if isinstance(val, float):
        return ("float", str(val), None, None)
    return ("str", str(val), None, len(str(val)))


def mut(seq, name, val, *, kind="local", key=None, line=None, file="prog.py", frame=1):
    return Mutation(
        seq=seq,
        kind=kind,
        name=name,
        key=key,
        value=_value_tuple(val),
        file=file,
        qualname="run",
        line=line if line is not None else 10 + seq,
        frame=frame,
    )


RUNNING_VALUES = [0, 5, 8, 16, 136, 140]


def running_recording():
    """A single local `running` accumulating: 0, 5, 8, 16, 136, 140."""
    return Recording([mut(i, "running", v, line=10 + i) for i, v in enumerate(RUNNING_VALUES)])


def cache_recording():
    """`running` locals then item writes to a `cache` container (for DAP)."""
    muts = [mut(i, "running", v, line=10 + i) for i, v in enumerate(RUNNING_VALUES)]
    items = [("5", 5), ("3", 8), ("8", 16), ("120", 136), ("4", 140)]
    for j, (k, v) in enumerate(items):
        muts.append(mut(6 + j, "cache", v, kind="item", key=k, line=20 + j))
    return Recording(muts)


def size_recording(keys):
    """Item writes to `cache` with the given key sequence (distinct-key count)."""
    return Recording([mut(i, "cache", i, kind="item", key=k, line=i) for i, k in enumerate(keys)])


@pytest.fixture
def rtt():
    return TimeTravel(running_recording())


# ===========================================================================
# coerce_value
# ===========================================================================


@pytest.mark.parametrize(
    "raw,expected",
    [
        (("int", "42", None, None), 42),
        (("int", "-7", None, None), -7),
        (("int", "0", None, None), 0),
        (("float", "3.5", None, None), 3.5),
        (("float", "-2.0", None, None), -2.0),
        (("bool", "True", None, None), True),
        (("bool", "False", None, None), False),
        (("bool", "true", None, None), False),  # exact match on "True" only
        (("none", "None", None, None), None),
        (("str", "hello", None, 5), "hello"),
        (("str", None, None, 0), ""),  # str with no repr -> ""
        (("int", "<int 5000 bits>", None, None), "<int 5000 bits>"),  # giant int -> repr
        (("float", "notafloat", None, None), "notafloat"),  # unparseable -> repr
        (("list", "[1, 2]", "list", 2), "[1, 2]"),  # containers -> repr string
        (("dict", None, "dict", 3), None),  # container with no repr -> None
    ],
)
def test_coerce_value(raw, expected):
    assert coerce_value(raw) == expected


# ===========================================================================
# parse_condition — predicate behavior (must NOT eval; bad compares -> False)
# ===========================================================================


@pytest.mark.parametrize(
    "expr,value,expected",
    [
        ("x > 100", 200, True),
        ("x > 100", 100, False),
        ("x > 100", 50, False),
        ("x > 100", "a string", False),  # str > int raises -> no match
        ("x >= 5", 5, True),
        ("x >= 5", 6, True),
        ("x >= 5", 4, False),
        ("x <= 5", 5, True),
        ("x <= 5", 4, True),
        ("x <= 5", 6, False),
        ("x < 5", 4, True),
        ("x < 5", 5, False),
        ("x == 16", 16, True),
        ("x == 16", 17, False),
        ("x != 16", 17, True),
        ("x != 16", 16, False),
        ("x == 'hello'", "hello", True),
        ("x == 'hello'", "bye", False),
        ('x == "quoted"', "quoted", True),
        ("x == true", True, True),
        ("x == true", False, False),
        ("x == false", False, True),
        ("x == none", None, True),
        ("x == none", 0, False),
        ("x == null", None, True),
        ("x == 3.5", 3.5, True),
        ("x == 3.5", 3.6, False),
        ("x != 'str'", 5, True),  # int != str is True, never raises
        ("x", 123, True),  # bare name fires on any write
        ("x", None, True),
        ("x", "whatever", True),
        ("x changed", 5, True),  # "name changed" fires on any write
        ("x changed", 0, True),
    ],
)
def test_parse_condition_predicate(expr, value, expected):
    _name, pred = parse_condition(expr)
    assert pred(value) is expected


@pytest.mark.parametrize(
    "expr,name",
    [
        ("running > 100", "running"),
        ("running", "running"),
        ("running changed", "running"),
        ("  spaced   == 5", "spaced"),
        ("a.b > 1", "a.b"),
        ("x>=1", "x"),
        ("cache != 0", "cache"),
        ("total < 10", "total"),
        ("n == none", "n"),
    ],
)
def test_parse_condition_name_extraction(expr, name):
    got, _pred = parse_condition(expr)
    assert got == name


@pytest.mark.parametrize("expr", ["x == ", "x > ", "x < ", "x >= ", "x <= "])
def test_parse_condition_empty_literal_never_matches_numbers(expr):
    # A trailing empty literal parses to the bare string "" — comparing it to an
    # int must never raise and (for equality/ordering ops) never match. (`!=`
    # legitimately returns True for int-vs-str, so it is excluded here.)
    _name, pred = parse_condition(expr)
    assert pred(5) is False


# ===========================================================================
# parse_len — the semantic size query  len(name) <op> N
# ===========================================================================


@pytest.mark.parametrize(
    "expr,name,n,passing,failing",
    [
        ("len(cache) > 100", "cache", 100, 101, 100),
        ("size(cache) >= 5", "cache", 5, 5, 4),
        ("len(x) <= 5", "x", 5, 5, 6),
        ("len(x) == 3", "x", 3, 3, 4),
        ("len(x) != 3", "x", 3, 4, 3),
        ("len(x) < 2", "x", 2, 1, 2),
        ("size(y) > 0", "y", 0, 1, 0),
        ("len( spaced ) > 1", "spaced", 1, 2, 1),
    ],
)
def test_parse_len_valid(expr, name, n, passing, failing):
    got_name, op, got_n = parse_len(expr)
    assert got_name == name
    assert got_n == n
    assert op(passing, n) is True
    assert op(failing, n) is False


@pytest.mark.parametrize(
    "expr",
    [
        "cache > 5",  # not a len()/size() query
        "len(cache)",  # no operator
        "len(cache) > abc",  # non-integer N
        "len(cache",  # unbalanced paren
        "x",
        "",
        "len(cache) ~ 5",  # unknown operator
        "running == 5",
    ],
)
def test_parse_len_rejects(expr):
    assert parse_len(expr) is None


# ===========================================================================
# TimeTravel — geometry, bounds, cursor
# ===========================================================================


def test_starts_at_end(rtt):
    assert rtt.pos == len(rtt) == 6
    assert rtt.at_end() and not rtt.at_start()
    assert rtt.current().value_repr == "140"


@pytest.mark.parametrize(
    "index,expected_index",
    [(-100, 0), (-1, 0), (0, 0), (2, 2), (3, 3), (5, 5), (6, 5), (100, 5)],
)
def test_goto_clamps(rtt, index, expected_index):
    step = rtt.goto(index)
    assert step is rtt.steps[expected_index]
    assert rtt.pos == expected_index + 1


def test_goto_on_empty_recording_returns_none():
    tt = TimeTravel(Recording([]))
    assert tt.goto(0) is None
    assert len(tt) == 0
    assert tt.current() is None


def test_step_forward_stops_at_end(rtt):
    rtt.goto(len(rtt) - 1)
    assert rtt.step_forward() is None
    assert rtt.at_end()


def test_step_back_stops_at_start(rtt):
    rtt.goto(0)
    assert rtt.step_back() is None
    assert rtt.at_start()
    assert rtt.pos == 0


def test_step_forward_and_back_walk(rtt):
    rtt.goto(2)
    assert rtt.step_forward().value_repr == RUNNING_VALUES[3].__str__()
    assert rtt.step_back().value_repr == str(RUNNING_VALUES[2])


@pytest.mark.parametrize(
    "pos,expected",
    [
        (0, None),
        (1, "0"),
        (2, "5"),
        (3, "8"),
        (4, "16"),
        (5, "136"),
        (6, "140"),
    ],
)
def test_state_at_reconstructs_every_position(rtt, pos, expected):
    state = rtt.state_at(pos)
    if expected is None:
        assert "running" not in state["locals"]
    else:
        assert state["locals"]["running"] == expected


@pytest.mark.parametrize("pos", [-5, 7, 100])
def test_state_at_clamps_out_of_range(rtt, pos):
    state = rtt.state_at(pos)
    # clamps to [0, N]; below 0 -> empty, above N -> final value
    if pos < 0:
        assert state["locals"] == {}
    else:
        assert state["locals"]["running"] == "140"


def test_state_reconstructs_containers():
    tt = TimeTravel(cache_recording())
    tt.goto(len(tt) - 1)
    end = tt.state()
    assert end["locals"]["running"] == "140"
    assert end["containers"]["cache"] == {"5": "5", "3": "8", "8": "16", "120": "136", "4": "140"}


# ===========================================================================
# TimeTravel — the query engine (find_all / find_first / find_last)
# ===========================================================================


@pytest.mark.parametrize(
    "expr,expected",
    [
        ("running", ["0", "5", "8", "16", "136", "140"]),
        ("running changed", ["0", "5", "8", "16", "136", "140"]),
        ("running > 100", ["136", "140"]),
        ("running >= 136", ["136", "140"]),
        ("running >= 16", ["16", "136", "140"]),
        ("running < 10", ["0", "5", "8"]),
        ("running <= 8", ["0", "5", "8"]),
        ("running == 16", ["16"]),
        ("running == 5", ["5"]),
        ("running != 16", ["0", "5", "8", "136", "140"]),
        ("running < 0", []),
        ("running > 1000", []),
        ("running > abc", []),  # malformed compare -> no crash, no match
        ("running == none", []),  # int == None -> False everywhere
        ("nonexistent > 5", []),  # unknown name -> nothing
    ],
)
def test_find_all_value_conditions(rtt, expr, expected):
    assert [s.value_repr for s in rtt.find_all(expr)] == expected


@pytest.mark.parametrize(
    "keys,expr,expected_count",
    [
        (["a", "b", "c", "d"], "len(cache) >= 3", 2),  # writes 3 & 4
        (["a", "b", "c", "d"], "len(cache) > 2", 2),
        (["a", "b", "c", "d"], "len(cache) == 2", 1),  # only the write reaching 2
        (["a", "b", "c", "d"], "len(cache) < 2", 1),  # only the first write (count 1)
        (["a", "b", "c", "d"], "len(cache) <= 2", 2),
        (["a", "b", "b", "c"], "len(cache) == 2", 2),  # repeated key holds at 2
        (["a", "b", "b", "c"], "len(cache) >= 2", 3),  # counts 2,2,3
        (["a", "b", "b", "c"], "len(cache) >= 3", 1),  # only the final distinct key
        (["a"], "len(cache) > 5", 0),
    ],
)
def test_find_all_size_query(keys, expr, expected_count):
    tt = TimeTravel(size_recording(keys))
    assert len(tt.find_all(expr)) == expected_count


def test_find_first_moves_cursor(rtt):
    step = rtt.find_first("running > 100")
    assert step.value_repr == "136"
    assert rtt.pos == step.index + 1
    assert rtt.state()["locals"]["running"] == "136"


def test_find_first_no_match_keeps_cursor(rtt):
    before = rtt.pos
    assert rtt.find_first("running > 100000") is None
    assert rtt.pos == before


def test_find_last_moves_to_last_match(rtt):
    step = rtt.find_last("running")
    assert step.value_repr == "140"
    assert rtt.pos == step.index + 1


def test_find_last_no_match_keeps_cursor(rtt):
    rtt.goto(2)
    before = rtt.pos
    assert rtt.find_last("running < -1") is None
    assert rtt.pos == before


# ===========================================================================
# TimeTravel — breakpoints, watchpoints, continue forward/back
# ===========================================================================


def test_watchpoint_continue_forward_and_back(rtt):
    rtt.goto(0)
    rtt.add_watchpoint("running > 100")
    hit = rtt.continue_forward()
    assert hit.value_repr == "136"
    nxt = rtt.continue_forward()
    assert nxt.value_repr == "140"
    assert rtt.continue_forward() is None  # nothing after -> end
    assert rtt.at_end()
    back = rtt.continue_back()
    assert back.value_repr == "136"


def test_continue_back_to_start_when_no_earlier_bp(rtt):
    rtt.goto(len(rtt) - 1)
    rtt.add_watchpoint("running < -1")  # never matches
    assert rtt.continue_back() is None
    assert rtt.at_start()


def test_line_breakpoint_stops_on_line(rtt):
    target = rtt.steps[3]
    rtt.goto(0)
    rtt.set_line_breakpoints(target.file, [target.line])
    hit = rtt.continue_forward()
    assert hit is not None and hit.line == target.line


def test_set_line_breakpoints_replaces_for_same_file(rtt):
    rtt.set_line_breakpoints("prog.py", [10, 11])
    rtt.set_line_breakpoints("prog.py", [12])  # replaces the prior two
    rtt.goto(0)
    hit = rtt.continue_forward()
    assert hit is not None and hit.line == 12


def test_clear_breakpoints_and_watchpoints(rtt):
    rtt.add_line_breakpoint("prog.py", 12)
    rtt.add_watchpoint("running > 0")
    rtt.clear_line_breakpoints()
    rtt.clear_watchpoints()
    rtt.goto(0)
    assert rtt.continue_forward() is None  # no breakpoints -> runs to end
    assert rtt.at_end()


@pytest.mark.parametrize(
    "line,should_match",
    [(13, True), (10, False), (99, False), (0, False)],
)
def test_line_breakpoint_matches(line, should_match):
    s = Step(
        index=0, seq=0, kind="local", name="x", key=None, file="prog.py",
        qualname="run", line=13, frame=1, value_repr="1", raw=("int", "1", None, None),
    )
    bp = LineBreakpoint("prog.py", line)
    assert bp.matches(s) is should_match


def test_watchpoint_matches_only_on_name_and_predicate():
    s = Step(
        index=0, seq=0, kind="local", name="running", key=None, file="prog.py",
        qualname="run", line=13, frame=1, value_repr="200", raw=("int", "200", None, None),
    )
    name, pred = parse_condition("running > 100")
    assert Watchpoint(name, pred).matches(s) is True
    other_name, other_pred = parse_condition("other > 100")
    assert Watchpoint(other_name, other_pred).matches(s) is False


# ===========================================================================
# record() / watch() — real capture semantics
# ===========================================================================


def test_record_captures_local_rebinds(tmp_path):
    out = tmp_path / "r.flight"
    with flight.record(path=out):
        total = 0
        total = total + 5
        total = total * 2  # 10
    rec = flight.read(out).recording()
    assert [m.value_repr for m in rec.history("total")] == ["0", "5", "10"]


def test_record_is_opt_in_scope_delimited(tmp_path):
    out = tmp_path / "scope.flight"

    def never_called():
        untouched = 999  # noqa: F841 — a frame we never enter must not appear

    with flight.record(path=out):
        inside = 222  # noqa: F841
    after = 333  # noqa: F841 — assigned after the scope closed, not captured
    names = flight.read(out).recording().names()
    assert "inside" in names
    assert "untouched" not in names  # code outside the scope isn't recorded
    assert "after" not in names


def test_watch_helper_is_noop_outside_scope():
    obj = {}
    assert flight.watch(obj) is obj
    assert flight.watch(obj, name="labelled") is obj


def test_watch_dict_item_writes(tmp_path):
    out = tmp_path / "d.flight"
    with flight.record(path=out) as rec:
        cache: dict = {}
        rec.watch(cache, name="cache")
        for i in range(3):
            cache[i] = i * i
        touch = True  # noqa: F841 — trailing line to flush the last diff
    writes = flight.read(out).recording().who_mutated("cache")
    keys = {m.key for m in writes}
    assert {"0", "1", "2"} <= keys
    last2 = [m for m in writes if m.key == "2"][-1]
    assert last2.value_repr == "4"


def test_watch_dict_deletion_records_deleted(tmp_path):
    out = tmp_path / "del.flight"
    with flight.record(path=out) as rec:
        d: dict = {}
        rec.watch(d, name="d")
        d["a"] = 1
        d["b"] = 2
        del d["a"]
        flush = 0  # noqa: F841 — trailing line so the deletion diff fires
    writes = flight.read(out).recording().who_mutated("d")
    assert any(m.key == "a" and m.value_repr == "<deleted>" for m in writes)


def test_watch_list_item_writes(tmp_path):
    out = tmp_path / "l.flight"
    with flight.record(path=out) as rec:
        lst: list = []
        rec.watch(lst, name="lst")
        lst.append(10)
        lst.append(20)
        lst[0] = 99
        flush = 0  # noqa: F841
    writes = flight.read(out).recording().who_mutated("lst")
    by_key = {}
    for m in writes:
        by_key.setdefault(m.key, []).append(m.value_repr)
    assert by_key["0"][-1] == "99"
    assert "20" in by_key["1"]


def test_watch_object_attribute_writes(tmp_path):
    out = tmp_path / "obj.flight"

    class Box:
        pass

    with flight.record(path=out) as rec:
        box = Box()
        rec.watch(box, name="box")
        box.value = 10
        box.value = 20
        flush = 0  # noqa: F841
    writes = flight.read(out).recording().who_mutated("box")
    reprs = [m.value_repr for m in writes if m.key == "value"]
    assert reprs == ["10", "20"]


def test_watch_set_is_not_captured_documented_limitation(tmp_path):
    # A set is neither dict nor list and has no __dict__, so the non-invasive
    # snapshot-diff captures nothing. This documents that known limitation.
    out = tmp_path / "set.flight"
    with flight.record(path=out) as rec:
        s: set = set()
        rec.watch(s, name="s")
        s.add(1)
        s.add(2)
        flush = 0  # noqa: F841
    writes = flight.read(out).recording().who_mutated("s")
    assert writes == []


def test_watch_via_module_helper_inside_scope(tmp_path):
    out = tmp_path / "mod.flight"
    with flight.record(path=out):
        data: dict = {}
        flight.watch(data, name="data")
        data["x"] = 99
        flush = 0  # noqa: F841
    writes = flight.read(out).recording().who_mutated("data")
    assert any(m.key == "x" and m.value_repr == "99" for m in writes)


@pytest.mark.parametrize(
    "varname,secret",
    [
        ("password", "hunter2"),
        ("api_key", "sk-123"),
        ("auth_token", "abc"),
        ("session", "sess"),
        ("credential", "cred"),
        ("ssn", "000-00-0000"),
    ],
)
def test_scrubbing_of_sensitive_local_names(tmp_path, varname, secret):
    out = tmp_path / "scrub.flight"
    src = (
        "import flight\n"
        f"with flight.record(path={str(out)!r}):\n"
        f"    {varname} = {secret!r}\n"
        "    _flush = 0\n"
    )
    ns: dict = {}
    exec(compile(src, "<scrubtest>", "exec"), ns)  # noqa: S102
    rec = flight.read(out).recording()
    hist = rec.history(varname)
    assert hist and hist[-1].value_repr == "<redacted>"


def test_scrubbing_of_sensitive_dict_keys(tmp_path):
    out = tmp_path / "scrubkey.flight"
    with flight.record(path=out) as rec:
        creds: dict = {}
        rec.watch(creds, name="creds")
        creds["password"] = "hunter2"
        creds["username"] = "alice"
        flush = 0  # noqa: F841
    writes = flight.read(out).recording().who_mutated("creds")
    by_key = {m.key: m.value_repr for m in writes}
    assert by_key["password"] == "<redacted>"
    assert by_key["username"] == "alice"


def test_exception_inside_scope_still_writes(tmp_path):
    out = tmp_path / "boom.flight"
    with pytest.raises(ZeroDivisionError):
        with flight.record(path=out):
            step = 0
            step = 1  # noqa: F841
            1 / 0
    rec = flight.read(out).recording()
    assert [m.value_repr for m in rec.history("step")][:2] == ["0", "1"]


def test_nested_scopes_independent(tmp_path):
    outer = tmp_path / "outer.flight"
    inner = tmp_path / "inner.flight"
    with flight.record(path=outer):
        a = 1  # noqa: F841
        with flight.record(path=inner):
            b = 2  # noqa: F841
        c = 3  # noqa: F841
    inner_names = flight.read(inner).recording().names()
    outer_names = flight.read(outer).recording().names()
    assert "b" in inner_names
    assert "a" in outer_names and "c" in outer_names
    # each scope wrote its own independent .flight (both are readable)
    assert flight.read(inner).has_mutations and flight.read(outer).has_mutations


def test_mutation_cap_truncates(tmp_path):
    out = tmp_path / "cap.flight"
    flight.install(config=Config(capture_max_mutations=5))
    try:
        with flight.record(path=out):
            acc = 0
            for i in range(50):
                acc = acc + i
    finally:
        flight.uninstall()
    f = flight.read(out)
    rec = f.recording()
    assert len(rec.mutations) <= 5


@pytest.mark.parametrize("seq_index", [0, 1, 2, 3])
def test_state_at_across_many_mutations(tmp_path, seq_index):
    out = tmp_path / "many.flight"
    with flight.record(path=out):
        n = 0
        n = 10
        n = 20
        n = 30
    rec = flight.read(out).recording()
    hist = rec.history("n")
    expected = ["0", "10", "20", "30"]
    target = hist[seq_index]
    state = rec.state_at(target.seq)
    assert state["n"] == expected[seq_index]


def test_who_mutated_orders_writes(tmp_path):
    out = tmp_path / "order.flight"
    with flight.record(path=out) as rec:
        counts: dict = {}
        rec.watch(counts, name="counts")
        counts["k"] = 1
        counts["k"] = 2
        counts["k"] = 3
        flush = 0  # noqa: F841
    writes = [m.value_repr for m in flight.read(out).recording().who_mutated("counts") if m.key == "k"]
    assert writes == ["1", "2", "3"]


# ===========================================================================
# DAP adapter
# ===========================================================================


class _Client:
    def __init__(self, adapter):
        self.a = adapter
        self.n = 0

    def __call__(self, command, **arguments):
        self.n += 1
        return self.a.handle(
            {"seq": self.n, "type": "request", "command": command, "arguments": arguments}
        )


def _events(msgs):
    return [m["event"] for m in msgs if m["type"] == "event"]


@pytest.fixture
def adapter():
    """A DebugAdapter with a synthetic recording loaded (cursor at the end)."""
    return DebugAdapter(TimeTravel(cache_recording()), path="synthetic.flight")


@pytest.fixture
def client(adapter):
    return _Client(adapter)


def _record_file(tmp_path, watch_cache=False):
    out = tmp_path / "dap.flight"
    with flight.record(path=str(out)) as rec:
        cache: dict = {}
        if watch_cache:
            rec.watch(cache, name="cache")
        running = 0
        for it in [5, 3, 8, 120, 4]:
            running = running + it
            cache[it] = running
    return str(out)


@pytest.mark.parametrize(
    "cap",
    [
        "supportsStepBack",
        "supportsConfigurationDoneRequest",
        "supportsConditionalBreakpoints",
        "supportsEvaluateForHovers",
        "supportsDataBreakpoints",
        "supportsTerminateRequest",
    ],
)
def test_initialize_capabilities(cap):
    msgs = _Client(DebugAdapter())("initialize")
    assert msgs[0]["body"][cap] is True
    assert "initialized" in _events(msgs)


@pytest.mark.parametrize(
    "command,arguments",
    [
        ("configurationDone", {}),
        ("threads", {}),
        ("stackTrace", {}),
        ("scopes", {}),
        ("variables", {"variablesReference": 1}),
        ("continue", {}),
        ("reverseContinue", {}),
        ("next", {}),
        ("stepIn", {}),
        ("stepOut", {}),
        ("stepBack", {}),
        ("pause", {}),
        ("setBreakpoints", {"source": {"path": "prog.py"}, "breakpoints": [{"line": 10}]}),
        ("dataBreakpointInfo", {"name": "running"}),
        ("setDataBreakpoints", {"breakpoints": [{"dataId": "running"}]}),
        ("evaluate", {"expression": "state"}),
        ("disconnect", {}),
        ("terminate", {}),
    ],
)
def test_command_returns_well_formed_response(client, command, arguments):
    msgs = client(command, **arguments)
    resp = msgs[0]
    assert resp["type"] == "response"
    assert resp["command"] == command
    assert resp["success"] is True
    assert "request_seq" in resp


@pytest.mark.parametrize(
    "command",
    ["continue", "reverseContinue", "next", "stepIn", "stepOut", "stepBack", "pause"],
)
def test_navigation_commands_emit_stopped(client, command):
    assert "stopped" in _events(client(command))


def test_unknown_command_fails_gracefully(client):
    resp = client("noSuchCommand")[0]
    assert resp["success"] is False
    assert "unsupported" in resp["message"]


def test_threads_shape(client):
    threads = client("threads")[0]["body"]["threads"]
    assert threads == [{"id": 1, "name": "flight (recorded)"}]


def test_scopes_shape(client):
    scopes = client("scopes")[0]["body"]["scopes"]
    assert {s["name"] for s in scopes} == {"Locals", "Containers"}


def test_stacktrace_reflects_cursor(client):
    # Cursor starts at the end: the last recorded write is the final cache item
    # (line 20+4 = 24). The frame name embeds the step's seq and target.
    frame = client("stackTrace")[0]["body"]["stackFrames"][0]
    assert frame["line"] == 24
    assert "cache" in frame["name"] and "#" in frame["name"]


def test_variables_locals_reference(client):
    variables = client("variables", variablesReference=1)[0]["body"]["variables"]
    assert any(v["name"] == "running" and v["value"] == "140" for v in variables)


def test_variables_containers_reference(client):
    containers = client("variables", variablesReference=2)[0]["body"]["variables"]
    cache = next(v for v in containers if v["name"] == "cache")
    assert "keys" in cache["value"]
    assert cache["variablesReference"] >= 100


def test_variables_container_items(client):
    containers = client("variables", variablesReference=2)[0]["body"]["variables"]
    ref = next(v["variablesReference"] for v in containers if v["name"] == "cache")
    items = client("variables", variablesReference=ref)[0]["body"]["variables"]
    assert any(v["name"] == "[5]" and v["value"] == "5" for v in items)


def test_variables_unknown_reference_is_empty(client):
    out = client("variables", variablesReference=9999)[0]["body"]["variables"]
    assert out == []


@pytest.mark.parametrize(
    "expr,substring,moved",
    [
        ("find running > 100", "136", True),
        ("past running > 100", "136", True),
        ("findlast running", "140", True),
        ("goto 0", "#0", True),
        ("goto abc", "expects an index", False),
        ("history running", "0", False),
        ("state", "running=", False),
        ("running", "140", False),  # bare name -> its value at the cursor
        ("find running > 100000", "no write matched", False),
        ("zzz_unknown", "?:", False),  # unknown -> treated as condition, no match
    ],
)
def test_evaluate(client, expr, substring, moved):
    msgs = client("evaluate", expression=expr)
    assert substring in msgs[0]["body"]["result"]
    assert ("stopped" in _events(msgs)) is moved


def test_evaluate_without_recording_fails():
    c = _Client(DebugAdapter())  # no tt loaded
    resp = c("evaluate", expression="state")[0]
    assert resp["success"] is False


@pytest.mark.parametrize("command", ["continue", "reverseContinue", "next", "stepBack"])
def test_navigation_without_recording_fails(command):
    c = _Client(DebugAdapter())
    resp = c(command)[0]
    assert resp["success"] is False
    assert "no recording" in resp["message"]


def test_setBreakpoints_verified_with_recording(client):
    body = client(
        "setBreakpoints", source={"path": "prog.py"}, breakpoints=[{"line": 12}]
    )[0]["body"]
    assert body["breakpoints"][0]["verified"] is True
    assert body["breakpoints"][0]["line"] == 12


def test_setBreakpoints_unverified_without_recording():
    c = _Client(DebugAdapter())
    body = c("setBreakpoints", source={"path": "p.py"}, breakpoints=[{"line": 3}])[0]["body"]
    assert body["breakpoints"][0]["verified"] is False


def test_setBreakpoints_conditional_adds_watchpoint(adapter):
    c = _Client(adapter)
    c("setBreakpoints", source={"path": "prog.py"}, breakpoints=[{"line": 1, "condition": "running > 100"}])
    adapter._tt.goto(0)
    hit = adapter._tt.continue_forward()
    assert hit is not None  # the conditional watchpoint fires


def test_setBreakpoints_lines_fallback(client):
    body = client("setBreakpoints", source={"path": "prog.py"}, lines=[10, 11])[0]["body"]
    assert [b["line"] for b in body["breakpoints"]] == [10, 11]


def test_dataBreakpointInfo(client):
    body = client("dataBreakpointInfo", name="running")[0]["body"]
    assert body["dataId"] == "running"
    assert body["accessTypes"] == ["write"]


def test_dataBreakpointInfo_no_name(client):
    body = client("dataBreakpointInfo")[0]["body"]
    assert body["dataId"] is None
    assert body["description"] == "no data"


def test_setDataBreakpoints_installs_watchpoint(adapter):
    c = _Client(adapter)
    out = c("setDataBreakpoints", breakpoints=[{"dataId": "running", "condition": "running > 100"}])[0]["body"]
    assert out["breakpoints"][0]["verified"] is True
    adapter._tt.goto(0)
    assert adapter._tt.continue_forward() is not None


def test_launch_loads_and_stops(tmp_path):
    path = _record_file(tmp_path)
    c = _Client(DebugAdapter())
    c("initialize")
    msgs = c("launch", program=path)
    assert msgs[0]["success"] is True
    assert "stopped" in _events(msgs)


def test_attach_is_launch(tmp_path):
    path = _record_file(tmp_path)
    c = _Client(DebugAdapter())
    c("initialize")
    msgs = c("attach", path=path)
    assert msgs[0]["success"] is True
    assert "stopped" in _events(msgs)


def test_launch_without_program_fails():
    c = _Client(DebugAdapter())
    c("initialize")
    resp = c("launch")[0]
    assert resp["success"] is False


def test_disconnect_stops_running(adapter):
    assert adapter.running is True
    _Client(adapter)("disconnect")
    assert adapter.running is False


# ===========================================================================
# Content-Length framing / serve
# ===========================================================================


@pytest.mark.parametrize(
    "msg",
    [
        {"seq": 1, "type": "event", "event": "hello"},
        {"seq": 2, "type": "response", "command": "initialize", "success": True},
        {"seq": 3, "type": "request", "command": "launch", "arguments": {"program": "x.flight"}},
        {"type": "event", "event": "unicode", "body": {"text": "café ☕ 日本語"}},
        {"nested": {"a": [1, 2, 3], "b": {"c": None}}},
    ],
)
def test_framing_round_trip(msg):
    buf = io.BytesIO()
    write_message(buf, msg)
    buf.seek(0)
    assert read_message(buf) == msg


def test_write_message_content_length_is_byte_count():
    buf = io.BytesIO()
    msg = {"text": "café"}  # multibyte -> byte length != char length
    write_message(buf, msg)
    raw = buf.getvalue()
    header, _, body = raw.partition(b"\r\n\r\n")
    declared = int(header.split(b":")[1].strip())
    assert declared == len(body)


def test_read_multiple_messages_sequentially():
    buf = io.BytesIO()
    write_message(buf, {"seq": 1, "type": "event", "event": "a"})
    write_message(buf, {"seq": 2, "type": "event", "event": "b"})
    buf.seek(0)
    assert read_message(buf)["event"] == "a"
    assert read_message(buf)["event"] == "b"
    assert read_message(buf) is None  # stream exhausted


def test_read_message_empty_stream_returns_none():
    assert read_message(io.BytesIO(b"")) is None


def test_read_message_missing_content_length_returns_none():
    assert read_message(io.BytesIO(b"Foo: bar\r\n\r\n")) is None


def test_read_message_zero_length_returns_none():
    assert read_message(io.BytesIO(b"Content-Length: 0\r\n\r\n")) is None


def _frame(msg: dict) -> bytes:
    data = json.dumps(msg).encode()
    return f"Content-Length: {len(data)}\r\n\r\n".encode() + data


def test_serve_over_streams(tmp_path):
    path = _record_file(tmp_path)
    instream = io.BytesIO(
        _frame({"seq": 1, "type": "request", "command": "initialize", "arguments": {}})
        + _frame({"seq": 2, "type": "request", "command": "launch", "arguments": {"program": path}})
        + _frame({"seq": 3, "type": "request", "command": "disconnect", "arguments": {}})
    )
    out = io.BytesIO()
    serve(instream, out, DebugAdapter())
    out.seek(0)
    events, responses = [], []
    while True:
        m = read_message(out)
        if m is None:
            break
        (events if m["type"] == "event" else responses).append(m)
    assert any(r["command"] == "initialize" and r["success"] for r in responses)
    assert any(e["event"] == "stopped" for e in events)


def test_serve_stops_on_disconnect(tmp_path):
    instream = io.BytesIO(
        _frame({"seq": 1, "type": "request", "command": "initialize", "arguments": {}})
        + _frame({"seq": 2, "type": "request", "command": "disconnect", "arguments": {}})
        # a trailing message that must NOT be processed after disconnect
        + _frame({"seq": 3, "type": "request", "command": "threads", "arguments": {}})
    )
    out = io.BytesIO()
    serve(instream, out, DebugAdapter())
    out.seek(0)
    commands = []
    while True:
        m = read_message(out)
        if m is None:
            break
        if m["type"] == "response":
            commands.append(m["command"])
    assert "threads" not in commands  # loop exited on disconnect
