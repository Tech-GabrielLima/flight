from __future__ import annotations

import sys
from dataclasses import dataclass, field
from typing import Any, Optional

from ._nondet import ReplayDivergence

_PEP667 = sys.version_info >= (3, 13)


def _safe_repr(value: Any, limit: int = 200) -> str:
    try:
        r = repr(value)
    except BaseException as e:
        return f"<repr failed: {type(e).__name__}>"
    return r if len(r) <= limit else r[:limit] + "…"


@dataclass
class Override:

    var: str
    value: Any
    line: int
    qualname: Optional[str] = None
    nth: int = 1
    applied: bool = field(default=False, compare=False)
    previous: Optional[str] = field(default=None, compare=False)

    def describe(self) -> str:
        where = f"{self.qualname}:{self.line}" if self.qualname else f"line {self.line}"
        was = f" (was {self.previous})" if self.previous is not None else ""
        return f"{self.var} := {_safe_repr(self.value)} at {where}{was}"


@dataclass
class Outcome:

    returned: Any = None
    exception: Optional[BaseException] = None
    diverged: bool = False

    @property
    def raised(self) -> bool:
        return self.exception is not None and not self.diverged

    def key(self):
        if self.diverged:
            return ("diverged",)
        if self.exception is not None:
            return ("raised", type(self.exception).__name__, str(self.exception))
        return ("returned", _safe_repr(self.returned))

    def describe(self) -> str:
        if self.diverged:
            return "diverged from the recorded run (a different path through the recorded world)"
        if self.exception is not None:
            return f"raised {type(self.exception).__name__}: {self.exception}"
        return f"returned {_safe_repr(self.returned)}"


@dataclass
class WhatIf:

    baseline: Outcome
    counterfactual: Outcome
    overrides: list[Override]

    @property
    def changed(self) -> bool:
        return self.baseline.key() != self.counterfactual.key()

    @property
    def unreached(self) -> list[Override]:
        return [o for o in self.overrides if not o.applied]

    def render(self) -> str:
        lines = ["what-if:"]
        for o in self.overrides:
            miss = "" if o.applied else "   ⚠ never reached"
            lines.append(f"  · {o.describe()}{miss}")
        lines.append(f"  before: {self.baseline.describe()}")
        lines.append(f"  after:  {self.counterfactual.describe()}")
        if not _PEP667:
            lines.append("  (note: live-local override needs Python 3.13+ — outcome unchanged here)")
        elif self.changed:
            lines.append("  → the change alters the outcome.")
        else:
            lines.append("  → no change to the outcome.")
        return "\n".join(lines)


def _make_tracer(overrides: list[Override]):
    counts: dict[int, int] = {}

    def tracer(frame, event, _arg):
        if event == "call":
            return tracer
        if event == "line":
            code = frame.f_code
            for ov in overrides:
                if ov.qualname is not None and code.co_qualname != ov.qualname:
                    continue
                if frame.f_lineno != ov.line:
                    continue
                key = id(ov)
                counts[key] = counts.get(key, 0) + 1
                if counts[key] == ov.nth:
                    try:
                        ov.previous = _safe_repr(frame.f_locals.get(ov.var, "<undefined>"))
                        frame.f_locals[ov.var] = ov.value
                        ov.applied = True
                    except Exception:
                        pass
        return tracer

    return tracer


def _run(tape, fn, args, kwargs, tracer) -> Outcome:
    from ._nondet import replay_tape

    target = fn
    if tracer is not None:

        def traced(*a, **k):
            old = sys.gettrace()
            sys.settrace(tracer)
            try:
                return fn(*a, **k)
            finally:
                sys.settrace(old)

        target = traced

    try:
        result = replay_tape(tape, target, *args, **kwargs)
        return Outcome(returned=result)
    except ReplayDivergence:
        return Outcome(diverged=True)
    except BaseException as e:
        return Outcome(exception=e)


def what_if(flight_path, fn, overrides, *args, **kwargs) -> WhatIf:
    from ._read import read

    if isinstance(overrides, Override):
        overrides = [overrides]
    overrides = list(overrides)

    baseline = _run(read(flight_path).tape(), fn, args, kwargs, tracer=None)
    counterfactual = _run(read(flight_path).tape(), fn, args, kwargs, tracer=_make_tracer(overrides))
    return WhatIf(baseline=baseline, counterfactual=counterfactual, overrides=overrides)


def run_whatif(flight_path, var, value, line, *, nth=1, qualname=None) -> dict:
    from ._generalize import load_invocable
    from ._read import read

    fl = read(flight_path)
    invoke = load_invocable(fl)
    if invoke is None:
        return {"ok": False, "error": "could not resolve the crash function from the recording"}
    ov = Override(var=var, value=value, line=int(line), qualname=qualname, nth=int(nth))
    try:
        wi = what_if(flight_path, invoke, ov)
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    return {
        "ok": True,
        "applied": ov.applied,
        "previous": ov.previous,
        "changed": wi.changed,
        "baseline": wi.baseline.describe(),
        "counterfactual": wi.counterfactual.describe(),
        "pep667": _PEP667,
    }


def _whatif_page(flight_path) -> str:
    from ._read import read

    fl = read(flight_path)
    crash = fl.crash() if fl.has_crash else None
    frame = crash.frames[0] if (crash and crash.frames) else None

    def esc(s):
        return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    rows = ""
    line = frame.lineno if frame else 0
    if frame:
        for name, oid in frame.locals:
            if name.startswith("__") and name.endswith("__"):
                continue
            rows += (
                f"<tr><td><code>{esc(name)}</code></td>"
                f"<td class=muted>{esc(crash.render(oid))}</td></tr>"
            )
    where = f"{frame.qualname} (line {frame.lineno})" if frame else "(no crash frame)"
    return f"""<!doctype html><meta charset=utf-8><title>flight what-if</title>
<style>
  :root{{color-scheme:light dark}}
  body{{font:14px/1.6 ui-monospace,Menlo,Consolas,monospace;margin:0;padding:24px;
        max-width:820px;background:Canvas;color:CanvasText}}
  h1{{font-size:18px}} h2{{font-size:13px;text-transform:uppercase;opacity:.7}}
  table{{width:100%;border-collapse:collapse}} td{{padding:4px 8px;border-bottom:1px solid #8883}}
  .muted{{opacity:.7}} code{{background:#8881;padding:1px 4px;border-radius:4px}}
  input,button{{font:inherit;padding:5px 8px;border-radius:6px;border:1px solid #8886;
    background:Canvas;color:CanvasText}}
  #res{{margin-top:16px;padding:12px;border:1px solid #8884;border-radius:8px;white-space:pre-wrap}}
  .b{{color:#3fb950;font-weight:700}} .a{{color:#d29922;font-weight:700}}
</style>
<h1>✈ flight what-if — {esc(where)}</h1>
<h2>crash-frame locals</h2>
<table>{rows or '<tr><td class=muted>no locals</td></tr>'}</table>
<h2>ask "what if…"</h2>
<p>Change a local at a line and see the counterfactual outcome, replayed over the
recorded world (time/random/IO held constant).</p>
<div>
  var <input id=var placeholder="numbers" size=12>
  := <input id=val placeholder="[1, 2, 3]  (JSON)" size=20>
  at line <input id=line type=number value="{line}" size=5>
  <button id=go>what if…</button>
</div>
<div id=res class=muted>baseline vs counterfactual will appear here.</div>
<script>
document.getElementById('go').addEventListener('click', async () => {{
  const res = document.getElementById('res');
  res.textContent = 'running…';
  let value; try {{ value = JSON.parse(document.getElementById('val').value); }}
  catch {{ value = document.getElementById('val').value; }}
  const body = {{ var: document.getElementById('var').value,
                  value, line: parseInt(document.getElementById('line').value,10) }};
  try {{
    const r = await fetch('/whatif', {{ method:'POST', headers:{{'Content-Type':'application/json'}},
                                       body: JSON.stringify(body) }});
    const d = await r.json();
    if (!d.ok) {{ res.textContent = 'error: ' + d.error; return; }}
    res.innerHTML = `<span class=b>before</span>  ${{d.baseline}}\\n` +
                    `<span class=a>after </span>  ${{d.counterfactual}}\\n\\n` +
                    (d.applied ? (d.changed ? '→ the change alters the outcome.'
                                            : '→ no change to the outcome.')
                               : '⚠ the override point was never reached.') +
                    (d.pep667 ? '' : '\\n(note: live-local override needs Python 3.13+)');
  }} catch (e) {{ res.textContent = 'error: ' + e; }}
}});
</script>
"""


def make_whatif_server(flight_path, host: str = "127.0.0.1", port: int = 8070):
    import json
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body: bytes, ctype="application/json"):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path.split("?")[0].rstrip("/") in ("", "/"):
                self._send(200, _whatif_page(flight_path).encode(), "text/html; charset=utf-8")
            else:
                self._send(404, b'{"error":"not found"}')

        def do_POST(self):
            if self.path.rstrip("/") != "/whatif":
                self._send(404, b'{"error":"not found"}')
                return
            length = int(self.headers.get("Content-Length", 0))
            try:
                req = json.loads(self.rfile.read(length) or b"{}")
            except Exception:
                self._send(400, b'{"ok":false,"error":"bad json"}')
                return
            out = run_whatif(
                flight_path, req.get("var", ""), req.get("value"),
                req.get("line", 0), nth=req.get("nth", 1), qualname=req.get("qualname"),
            )
            self._send(200 if out.get("ok") else 422, json.dumps(out).encode())

    return ThreadingHTTPServer((host, port), Handler)


def serve_whatif(flight_path, *, host: str = "127.0.0.1", port: int = 8070):
    server = make_whatif_server(flight_path, host, port)
    print(f"[flight] what-if console on http://{host}:{port}  ({flight_path})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
