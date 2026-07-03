"""Phase 5 — a Debug Adapter Protocol (DAP) server over the time-travel engine.

DAP is what VS Code and PyCharm speak to a debugger. It has *built-in* support
for reverse execution — a client that sees the ``supportsStepBack`` capability
shows "Step Back" and "Reverse" buttons and sends ``stepBack`` /
``reverseContinue`` requests. So exposing :mod:`flight._timetravel` over DAP hands
flight a real reverse-debugging UI in those editors for free, with the recorded
locals, the mutation timeline and the "breakpoint in the past" all navigable.

The adapter is a **read-only** debugger over a `.flight` scope recording: there
is no live process, `continue`/`stepBack`/`reverseContinue` walk the recorded
timeline, `variables` reconstructs the state at the cursor, and `evaluate` is a
small REPL (``find running > 100`` jumps to the write that matched — the
breakpoint in the past). :meth:`DebugAdapter.handle` is pure (request dict →
list of message dicts), so the protocol is unit-tested without an editor or a
socket; :func:`serve` adds the Content-Length framing over a byte stream.
"""

from __future__ import annotations

import json
import os
from typing import Optional

from ._timetravel import TimeTravel

THREAD_ID = 1
LOCALS_REF = 1
CONTAINERS_REF = 2
_CONTAINER_BASE = 100  # container #i lives at reference _CONTAINER_BASE + i


class DebugAdapter:
    """Turns DAP requests into responses + events over a `TimeTravel` engine."""

    def __init__(self, tt: Optional[TimeTravel] = None, path: str = ""):
        self._tt = tt
        self._path = path
        self._seq = 0
        self.running = True

    # -- message helpers ----------------------------------------------------

    def _mseq(self) -> int:
        self._seq += 1
        return self._seq

    def _response(self, req: dict, body=None, success: bool = True, message: str = "") -> dict:
        msg = {
            "seq": self._mseq(),
            "type": "response",
            "request_seq": req.get("seq", 0),
            "success": success,
            "command": req.get("command", ""),
        }
        if body is not None:
            msg["body"] = body
        if message:
            msg["message"] = message
        return msg

    def _event(self, event: str, body=None) -> dict:
        msg = {"seq": self._mseq(), "type": "event", "event": event}
        if body is not None:
            msg["body"] = body
        return msg

    def _stopped(self, reason: str) -> dict:
        return self._event(
            "stopped",
            {"reason": reason, "threadId": THREAD_ID, "allThreadsStopped": True},
        )

    # -- dispatch -----------------------------------------------------------

    def handle(self, req: dict) -> list[dict]:
        command = req.get("command", "")
        handler = getattr(self, f"_cmd_{command}", None)
        if handler is None:
            return [self._response(req, success=False, message=f"unsupported: {command}")]
        try:
            return handler(req)
        except Exception as e:  # never crash the adapter (P1)
            return [self._response(req, success=False, message=f"{type(e).__name__}: {e}")]

    # -- lifecycle ----------------------------------------------------------

    def _cmd_initialize(self, req: dict) -> list[dict]:
        caps = {
            "supportsStepBack": True,
            "supportsConfigurationDoneRequest": True,
            "supportsConditionalBreakpoints": True,
            "supportsEvaluateForHovers": True,
            "supportsDataBreakpoints": True,
            "supportsTerminateRequest": True,
        }
        return [self._response(req, caps), self._event("initialized")]

    def _load(self, path: str) -> None:
        from ._read import read

        self._path = path
        self._tt = TimeTravel(read(path).recording())

    def _cmd_launch(self, req: dict) -> list[dict]:
        args = req.get("arguments", {})
        path = args.get("program") or args.get("path") or args.get("flight") or self._path
        if not path:
            return [self._response(req, success=False, message="no .flight program given")]
        self._load(path)
        return [self._response(req), self._stopped("entry")]

    _cmd_attach = _cmd_launch

    def _cmd_configurationDone(self, req: dict) -> list[dict]:
        return [self._response(req)]

    def _cmd_threads(self, req: dict) -> list[dict]:
        return [self._response(req, {"threads": [{"id": THREAD_ID, "name": "flight (recorded)"}]})]

    def _cmd_disconnect(self, req: dict) -> list[dict]:
        self.running = False
        return [self._response(req)]

    _cmd_terminate = _cmd_disconnect

    # -- inspection ---------------------------------------------------------

    def _cmd_stackTrace(self, req: dict) -> list[dict]:
        tt = self._tt
        step = tt.current() if tt else None
        if step is None:
            # Before the first write (or an empty recording): a synthetic frame.
            frame = {
                "id": 1,
                "name": "<recording start>",
                "line": tt.steps[0].line if tt and len(tt) else 0,
                "column": 1,
                "source": self._source(tt.steps[0].file if tt and len(tt) else self._path),
            }
        else:
            frame = {
                "id": 1,
                "name": f"{step.qualname}  [#{step.seq} · {step.target}]",
                "line": step.line,
                "column": 1,
                "source": self._source(step.file),
            }
        return [self._response(req, {"stackFrames": [frame], "totalFrames": 1})]

    def _source(self, path: str) -> dict:
        return {"name": os.path.basename(path) if path else "recording", "path": path}

    def _cmd_scopes(self, req: dict) -> list[dict]:
        scopes = [
            {"name": "Locals", "variablesReference": LOCALS_REF, "expensive": False},
            {"name": "Containers", "variablesReference": CONTAINERS_REF, "expensive": False},
        ]
        return [self._response(req, {"scopes": scopes})]

    def _cmd_variables(self, req: dict) -> list[dict]:
        ref = req.get("arguments", {}).get("variablesReference", 0)
        state = self._tt.state() if self._tt else {"locals": {}, "containers": {}}
        containers = sorted(state["containers"])
        out = []
        if ref == LOCALS_REF:
            for name in sorted(state["locals"]):
                out.append(self._var(name, state["locals"][name]))
        elif ref == CONTAINERS_REF:
            for i, name in enumerate(containers):
                out.append(
                    {
                        "name": name,
                        "value": f"{{{len(state['containers'][name])} keys}}",
                        "variablesReference": _CONTAINER_BASE + i,
                    }
                )
        elif ref >= _CONTAINER_BASE:
            i = ref - _CONTAINER_BASE
            if 0 <= i < len(containers):
                items = state["containers"][containers[i]]
                for key in items:
                    out.append(self._var(f"[{key}]", items[key]))
        return [self._response(req, {"variables": out})]

    @staticmethod
    def _var(name: str, value: str) -> dict:
        return {"name": name, "value": value, "variablesReference": 0}

    # -- navigation ---------------------------------------------------------

    def _need_tt(self, req: dict) -> Optional[list[dict]]:
        if self._tt is None:
            return [self._response(req, success=False, message="no recording loaded")]
        return None

    def _cmd_continue(self, req: dict) -> list[dict]:
        err = self._need_tt(req)
        if err:
            return err
        hit = self._tt.continue_forward()
        return [
            self._response(req, {"allThreadsContinued": True}),
            self._stopped("breakpoint" if hit else "pause"),
        ]

    def _cmd_reverseContinue(self, req: dict) -> list[dict]:
        err = self._need_tt(req)
        if err:
            return err
        hit = self._tt.continue_back()
        return [self._response(req), self._stopped("breakpoint" if hit else "pause")]

    def _cmd_next(self, req: dict) -> list[dict]:
        err = self._need_tt(req)
        if err:
            return err
        self._tt.step_forward()
        return [self._response(req), self._stopped("step")]

    _cmd_stepIn = _cmd_next
    _cmd_stepOut = _cmd_next

    def _cmd_stepBack(self, req: dict) -> list[dict]:
        err = self._need_tt(req)
        if err:
            return err
        self._tt.step_back()
        return [self._response(req), self._stopped("step")]

    def _cmd_pause(self, req: dict) -> list[dict]:
        return [self._response(req), self._stopped("pause")]

    # -- breakpoints --------------------------------------------------------

    def _cmd_setBreakpoints(self, req: dict) -> list[dict]:
        args = req.get("arguments", {})
        path = args.get("source", {}).get("path", "")
        bps = args.get("breakpoints") or [{"line": ln} for ln in args.get("lines", [])]
        verified = []
        if self._tt is not None:
            self._tt.set_line_breakpoints(path, [b["line"] for b in bps])
            for b in bps:
                cond = b.get("condition")
                if cond:
                    self._tt.add_watchpoint(cond)
        for b in bps:
            verified.append({"verified": self._tt is not None, "line": b.get("line", 0)})
        return [self._response(req, {"breakpoints": verified})]

    def _cmd_dataBreakpointInfo(self, req: dict) -> list[dict]:
        name = req.get("arguments", {}).get("name", "")
        return [
            self._response(
                req,
                {
                    "dataId": name or None,
                    "description": f"write to {name}" if name else "no data",
                    "accessTypes": ["write"],
                },
            )
        ]

    def _cmd_setDataBreakpoints(self, req: dict) -> list[dict]:
        args = req.get("arguments", {})
        out = []
        if self._tt is not None:
            self._tt.clear_watchpoints()
            for b in args.get("breakpoints", []):
                data_id = b.get("dataId", "")
                cond = b.get("condition")
                self._tt.add_watchpoint(cond if cond else data_id)
                out.append({"verified": True})
        return [self._response(req, {"breakpoints": out})]

    # -- REPL / evaluate ----------------------------------------------------

    def _cmd_evaluate(self, req: dict) -> list[dict]:
        args = req.get("arguments", {})
        expr = (args.get("expression") or "").strip()
        if self._tt is None:
            return [self._response(req, success=False, message="no recording loaded")]
        result, moved = self._evaluate(expr)
        msgs = [self._response(req, {"result": result, "variablesReference": 0})]
        if moved:
            msgs.append(self._stopped("goto"))  # cursor moved: refresh the UI
        return msgs

    def _evaluate(self, expr: str) -> tuple[str, bool]:
        tt = self._tt
        parts = expr.split(maxsplit=1)
        verb = parts[0].lower() if parts else ""
        rest = parts[1] if len(parts) > 1 else ""
        if verb in ("find", "past") and rest:
            step = tt.find_first(rest)
            return (f"→ {step.describe()}" if step else f"no write matched {rest!r}"), bool(step)
        if verb == "findlast" and rest:
            step = tt.find_last(rest)
            return (f"→ {step.describe()}" if step else f"no write matched {rest!r}"), bool(step)
        if verb == "goto" and rest:
            try:
                step = tt.goto(int(rest))
            except ValueError:
                return f"goto expects an index, got {rest!r}", False
            return (step.describe() if step else "empty recording"), bool(step)
        if verb == "history" and rest:
            hits = tt.find_all(rest)
            return (" ".join(s.value_repr for s in hits) or f"no writes to {rest!r}"), False
        if verb == "state":
            locs = tt.state()["locals"]
            return (", ".join(f"{k}={v}" for k, v in sorted(locs.items())) or "<empty>"), False
        # a bare name: its value at the cursor
        locs = tt.state()["locals"]
        if expr in locs:
            return locs[expr], False
        # otherwise treat the whole thing as a condition to jump to
        step = tt.find_first(expr)
        return (f"→ {step.describe()}" if step else f"?: {expr}"), bool(step)


# -- Content-Length framing (VS Code / DAP transport) -----------------------


def read_message(stream) -> Optional[dict]:
    """Read one DAP message (Content-Length framed) from a binary stream."""
    headers: dict[str, str] = {}
    while True:
        line = stream.readline()
        if not line:
            return None
        text = line.decode("ascii", "replace").strip()
        if text == "":
            break
        key, _, value = text.partition(":")
        headers[key.strip().lower()] = value.strip()
    length = int(headers.get("content-length", 0))
    if length <= 0:
        return None
    body = stream.read(length)
    return json.loads(body.decode("utf-8"))


def write_message(stream, msg: dict) -> None:
    data = json.dumps(msg).encode("utf-8")
    stream.write(f"Content-Length: {len(data)}\r\n\r\n".encode("ascii"))
    stream.write(data)
    stream.flush()


def serve(instream, outstream, adapter: Optional[DebugAdapter] = None) -> None:
    """Run a DAP session over two binary streams (stdin/stdout for an editor)."""
    adapter = adapter or DebugAdapter()
    while adapter.running:
        req = read_message(instream)
        if req is None:
            break
        for msg in adapter.handle(req):
            write_message(outstream, msg)
