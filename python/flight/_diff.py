from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class Divergence:

    kind: str
    identical: bool
    index: Optional[int]
    left: Optional[str]
    right: Optional[str]
    detail: str
    compared: int = 0

    def __bool__(self) -> bool:
        return not self.identical

    def render(self) -> str:
        head = f"comparing {self.kind}s"
        if self.identical:
            return f"{head}: identical ({self.compared} steps compared)"
        lines = [
            f"{head}: diverged at step {self.index} ({self.compared} steps compared)",
            f"  {self.detail}",
        ]
        if self.left is not None or self.right is not None:
            lines.append(f"  left : {self.left}")
            lines.append(f"  right: {self.right}")
        return "\n".join(lines)


def _identical(kind: str, compared: int) -> Divergence:
    return Divergence(kind, True, None, None, None, "the recordings match", compared)


def _mut_key(m) -> tuple:
    return (m.kind, m.name, m.key, m.line)


def diff_mutations(a, b) -> Divergence:
    ma, mb = a.mutations, b.mutations
    n = min(len(ma), len(mb))
    for i in range(n):
        x, y = ma[i], mb[i]
        if _mut_key(x) != _mut_key(y):
            return Divergence(
                "mutation", False, i, _render_mut(x), _render_mut(y),
                f"different write here ({_target(x)} vs {_target(y)})", n,
            )
        if x.value_repr != y.value_repr:
            return Divergence(
                "mutation", False, i, _render_mut(x), _render_mut(y),
                f"{_target(x)} = {x.value_repr!r} here but {y.value_repr!r} there", n,
            )
    if len(ma) != len(mb):
        longer = "left" if len(ma) > len(mb) else "right"
        extra = (ma if longer == "left" else mb)[n]
        return Divergence(
            "mutation", False, n,
            _render_mut(ma[n]) if len(ma) > n else None,
            _render_mut(mb[n]) if len(mb) > n else None,
            f"{longer} recording kept writing ({_render_mut(extra)})", n,
        )
    return _identical("mutation", n)


def _target(m) -> str:
    if m.kind == "local":
        return m.name
    return f"{m.name}[{m.key}]" if m.kind == "item" else f"{m.name}.{m.key}"


def _render_mut(m) -> str:
    return f"#{m.seq} {m.kind} {_target(m)} = {m.value_repr}"


def diff_tapes(a, b) -> Divergence:
    ra, rb = a.rows(), b.rows()
    n = min(len(ra), len(rb))
    for i in range(n):
        _sa, srca, taga, pa = ra[i]
        _sb, srcb, tagb, pb = rb[i]
        if srca != srcb:
            return Divergence(
                "nondet", False, i, srca, srcb,
                f"control flow branched: {srca} vs {srcb}", n,
            )
        if (taga, pa) != (tagb, pb):
            return Divergence(
                "nondet", False, i, _render_row(ra[i]), _render_row(rb[i]),
                f"{srca} answered differently", n,
            )
    if len(ra) != len(rb):
        longer = "left" if len(ra) > len(rb) else "right"
        return Divergence(
            "nondet", False, n,
            _render_row(ra[n]) if len(ra) > n else None,
            _render_row(rb[n]) if len(rb) > n else None,
            f"{longer} recording made more boundary calls", n,
        )
    return _identical("nondet", n)


def _render_row(row) -> str:
    _seq, src, tag, payload = row
    p = payload if len(payload) <= 40 else payload[:40] + "…"
    return f"{src} [{tag}] {p}"


def diff_events(a_events, b_events) -> Divergence:
    n = min(len(a_events), len(b_events))
    for i in range(n):
        if tuple(a_events[i]) != tuple(b_events[i]):
            return Divergence(
                "event", False, i, _render_event(a_events[i]), _render_event(b_events[i]),
                "execution path diverged here", n,
            )
    if len(a_events) != len(b_events):
        longer = "left" if len(a_events) > len(b_events) else "right"
        return Divergence("event", False, n, None, None, f"{longer} ran longer", n)
    return _identical("event", n)


def _render_event(e) -> str:
    kind, file, qual, line = e
    import os

    return f"{kind} {qual} ({os.path.basename(file)}:{line})"


def diff_files(path_a: str, path_b: str) -> Divergence:
    from ._read import read

    fa, fb = read(path_a), read(path_b)
    if fa.has_mutations and fb.has_mutations:
        return diff_mutations(fa.recording(), fb.recording())
    if fa.has_nondet and fb.has_nondet:
        return diff_tapes(fa.tape(), fb.tape())
    ea, eb = fa.events(), fb.events()
    if ea and eb:
        return diff_events(ea, eb)
    return Divergence(
        "incomparable", False, None, None, None,
        "the two files share no comparable axis (mutations / tape / events)", 0,
    )


def _axis_rows(fl):
    if fl.has_mutations:
        return "mutation", [_render_mut(m) for m in fl.recording().mutations]
    if fl.has_nondet:
        return "nondet", [_render_row(r) for r in fl.tape().rows()]
    return "event", [_render_event(e) for e in fl.events()]


def diff_html(path_a: str, path_b: str) -> str:
    import os

    from ._read import read

    fa, fb = read(path_a), read(path_b)
    div = diff_files(path_a, path_b)
    if fa.has_mutations and fb.has_mutations:
        kind = "mutation"
    elif fa.has_nondet and fb.has_nondet:
        kind = "nondet"
    else:
        kind = "event"
    _ka, rows_a = _axis_rows(fa)
    _kb, rows_b = _axis_rows(fb)

    def esc(s):
        return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    n = max(len(rows_a), len(rows_b))
    body = []
    for i in range(n):
        a = rows_a[i] if i < len(rows_a) else ""
        b = rows_b[i] if i < len(rows_b) else ""
        cls = " class=diverge" if (div.index is not None and i == div.index) else (
            "" if a == b else " class=differ"
        )
        body.append(
            f"<tr{cls}><td class=n>{i}</td><td>{esc(a)}</td><td>{esc(b)}</td></tr>"
        )

    headline = (
        f"identical on the {kind} axis ({div.compared} steps compared)"
        if div.identical
        else f"diverged at step {div.index} on the {kind} axis — {esc(div.detail)}"
    )
    return f"""<!doctype html><meta charset=utf-8><title>flight diff</title>
<style>
  :root{{color-scheme:light dark}}
  body{{font:13px/1.5 ui-monospace,Menlo,Consolas,monospace;margin:0;padding:24px;
        background:Canvas;color:CanvasText}}
  h1{{font-size:17px}} .headline{{margin:8px 0 16px;padding:8px 12px;border-radius:8px;
     background:#8881}} .headline.bad{{background:#ff7b7233;color:#ff7b72}}
  table{{width:100%;border-collapse:collapse}}
  td,th{{text-align:left;padding:4px 8px;border-bottom:1px solid #8883;vertical-align:top}}
  .n{{color:#8b949e;width:3em;text-align:right}}
  tr.differ td{{background:#d2992218}}
  tr.diverge td{{background:#ff7b7233;font-weight:600}}
  .cols th{{color:#8b949e}}
</style>
<h1>✈ flight diff</h1>
<div class="headline {'bad' if not div.identical else ''}">{headline}</div>
<table><tr class=cols><th class=n>#</th><th>{esc(os.path.basename(path_a))}</th>
<th>{esc(os.path.basename(path_b))}</th></tr>
{''.join(body)}</table>
"""
