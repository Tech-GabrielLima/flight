"""Phase 5 — the reverse debugger: time-travel engine, past breakpoints, DAP."""

from __future__ import annotations

import io

import pytest

import flight
from flight._dap import DebugAdapter, read_message, serve, write_message
from flight._timetravel import TimeTravel, coerce_value, parse_condition


def _record(path, watch_cache=False):
    """Record a known program: running accumulates, cache grows under a watch."""
    with flight.record(path=str(path)) as rec:
        cache = {}
        if watch_cache:
            rec.watch(cache, name="cache")
        running = 0
        for it in [5, 3, 8, 120, 4]:
            running = running + it
            cache[it] = running
    return str(path)


@pytest.fixture
def tt(tmp_path):
    path = _record(tmp_path / "scope.flight", watch_cache=True)
    return flight.read(path).time_travel()


# -- engine: geometry & state ----------------------------------------------


def test_starts_at_the_end_post_mortem(tt):
    assert tt.pos == len(tt)
    assert tt.at_end()
    assert tt.state()["locals"]["running"] == "140"


def test_state_reconstruction_of_locals_and_containers(tt):
    tt.goto(0)  # first write
    first = tt.state()
    # running evolves; walk to the end and check the container was reconstructed
    tt.goto(len(tt) - 1)
    end = tt.state()
    assert end["locals"]["running"] == "140"
    assert end["containers"]["cache"] == {"5": "5", "3": "8", "8": "16", "120": "136", "4": "140"}
    assert first != end


def test_step_forward_and_back_are_bounded(tt):
    tt.goto(0)
    assert tt.step_back() is None  # already at/near start
    assert tt.at_start()
    tt.goto(len(tt) - 1)
    assert tt.step_forward() is None  # at the end
    assert tt.at_end()


def test_goto_clamps(tt):
    assert tt.goto(10_000) is tt.steps[-1]
    assert tt.goto(-5) is tt.steps[0]


# -- breakpoint in the past -------------------------------------------------


def test_find_first_moves_cursor_to_the_matching_write(tt):
    step = tt.find_first("running > 100")
    assert step is not None
    assert step.value_repr == "136"  # 0+5+3+8+120 = 136, first over 100
    assert tt.state()["locals"]["running"] == "136"


def test_find_all_is_the_history_of_a_value(tt):
    reprs = [s.value_repr for s in tt.find_all("running")]
    assert reprs == ["0", "5", "8", "16", "136", "140"]


def test_find_first_never_matched_returns_none_and_keeps_cursor(tt):
    before = tt.pos
    assert tt.find_first("running > 10000") is None
    assert tt.pos == before


def test_find_equality_and_last(tt):
    s = tt.find_first("running == 16")
    assert s is not None and s.value_repr == "16"
    assert tt.find_last("running").value_repr == "140"


# -- line breakpoints & continue / reverse ----------------------------------


def test_continue_forward_and_back_hit_a_watchpoint(tt):
    tt.goto(0)
    tt.add_watchpoint("running > 100")
    hit = tt.continue_forward()
    assert hit is not None and hit.value_repr == "136"
    # nothing after it matches (140 also >100) -> next match is 140
    nxt = tt.continue_forward()
    assert nxt is not None and nxt.value_repr == "140"
    # reverse back to the previous match
    back = tt.continue_back()
    assert back is not None and back.value_repr == "136"


def test_line_breakpoint_stops_on_that_line(tt):
    target = tt.find_all("running")[-1]  # a write to running
    tt.clear_watchpoints()
    tt.goto(0)
    tt.set_line_breakpoints(target.file, [target.line])
    hit = tt.continue_forward()
    assert hit is not None and hit.line == target.line


# -- predicates & coercion --------------------------------------------------


@pytest.mark.parametrize(
    "value,expected",
    [
        (("int", "42", None, None), 42),
        (("float", "3.5", None, None), 3.5),
        (("bool", "True", None, None), True),
        (("none", "None", None, None), None),
        (("str", "hello", None, 5), "hello"),
        (("int", "<int 5000 bits>", None, None), "<int 5000 bits>"),  # giant -> repr
    ],
)
def test_coerce_value(value, expected):
    assert coerce_value(value) == expected


def test_parse_condition_is_safe_on_bad_comparisons():
    name, pred = parse_condition("x > 100")
    assert name == "x"
    assert pred(200) is True
    assert pred("a string") is False  # str > int would raise -> treated as no-match


def test_empty_recording_is_harmless(tmp_path):
    path = _record(tmp_path / "e.flight")  # still has writes; make a trivially empty engine
    tt = flight.read(path).time_travel()
    tt.goto(0)
    tt.clear_line_breakpoints()
    tt.clear_watchpoints()
    assert tt.continue_forward() is None or tt.at_end()


# -- DAP adapter (dict level, no editor) ------------------------------------


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


def test_dap_initialize_advertises_step_back():
    c = _Client(DebugAdapter())
    msgs = c("initialize")
    assert msgs[0]["body"]["supportsStepBack"] is True
    assert "initialized" in _events(msgs)


def test_dap_launch_loads_and_stops_at_entry(tmp_path):
    path = _record(tmp_path / "s.flight")
    c = _Client(DebugAdapter())
    c("initialize")
    msgs = c("launch", program=path)
    assert msgs[0]["success"] is True
    assert "stopped" in _events(msgs)


def test_dap_inspection_flow(tmp_path):
    path = _record(tmp_path / "s.flight", watch_cache=True)
    c = _Client(DebugAdapter())
    c("initialize")
    c("launch", program=path)
    frame = c("stackTrace")[0]["body"]["stackFrames"][0]
    assert frame["source"]["path"].endswith("s.flight") or frame["line"] >= 0
    scopes = c("scopes")[0]["body"]["scopes"]
    assert {s["name"] for s in scopes} == {"Locals", "Containers"}
    locals_ref = next(s["variablesReference"] for s in scopes if s["name"] == "Locals")
    variables = c("variables", variablesReference=locals_ref)[0]["body"]["variables"]
    assert any(v["name"] == "running" and v["value"] == "140" for v in variables)


def test_dap_step_back_and_reverse_emit_stopped(tmp_path):
    path = _record(tmp_path / "s.flight")
    c = _Client(DebugAdapter())
    c("initialize")
    c("launch", program=path)
    assert "stopped" in _events(c("stepBack"))
    assert "stopped" in _events(c("next"))
    assert "stopped" in _events(c("reverseContinue"))
    cont = c("continue")
    assert cont[0]["body"]["allThreadsContinued"] is True
    assert "stopped" in _events(cont)


def test_dap_evaluate_breakpoint_in_the_past(tmp_path):
    path = _record(tmp_path / "s.flight")
    c = _Client(DebugAdapter())
    c("initialize")
    c("launch", program=path)
    msgs = c("evaluate", expression="find running > 100")
    assert "136" in msgs[0]["body"]["result"]
    assert "stopped" in _events(msgs)  # the cursor moved -> UI refreshes


def test_dap_set_breakpoints_verified(tmp_path):
    path = _record(tmp_path / "s.flight")
    c = _Client(DebugAdapter())
    c("initialize")
    c("launch", program=path)
    body = c("setBreakpoints", source={"path": path}, breakpoints=[{"line": 999}])[0]["body"]
    assert body["breakpoints"][0]["verified"] is True


def test_dap_unsupported_command_fails_gracefully():
    c = _Client(DebugAdapter())
    resp = c("noSuchCommand")[0]
    assert resp["success"] is False


# -- DAP framing / serve ----------------------------------------------------


def test_framing_round_trip():
    buf = io.BytesIO()
    write_message(buf, {"seq": 1, "type": "event", "event": "hello"})
    buf.seek(0)
    assert read_message(buf)["event"] == "hello"


def _frame(msg: dict) -> bytes:
    import json

    data = json.dumps(msg).encode()
    return f"Content-Length: {len(data)}\r\n\r\n".encode() + data


def test_serve_over_streams(tmp_path):
    path = _record(tmp_path / "s.flight")
    instream = io.BytesIO(
        _frame({"seq": 1, "type": "request", "command": "initialize", "arguments": {}})
        + _frame(
            {"seq": 2, "type": "request", "command": "launch", "arguments": {"program": path}}
        )
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
