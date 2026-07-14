from __future__ import annotations

from pathlib import Path


def render_comment(flight_path, *, repro_hint: bool = True, title: str = "Flight — root cause") -> str:
    from ._explain import analyze, build_context
    from ._fingerprint import fingerprint
    from ._read import read

    name = Path(flight_path).name
    f = read(flight_path)
    lines: list[str] = [f"### ✈️ {title}"]

    if not f.has_crash:
        lines.append("")
        lines.append(f"`{name}` has no crash detail (ring-only snapshot).")
        return "\n".join(lines)

    exc_type, message, _rel = f.exceptions[0]
    lines.append("")
    lines.append(f"**`{exc_type}`**: {_md_escape(message)}")

    ctx = build_context(flight_path)
    _summary, suspects = analyze(ctx)

    cf = ctx.get("crash_frame")
    if cf:
        where = f"{Path(cf['file']).name}:{cf['line']}"
        lines.append("")
        lines.append(f"Crashed in `{cf['qualname']}` ({where}).")

    if suspects:
        lines.append("")
        lines.append("<details><summary>Likely cause</summary>")
        lines.append("")
        for s in suspects:
            lines.append(f"- {_md_escape(s)}")
        lines.append("")
        lines.append("</details>")

    stack = ctx.get("stack") or []
    if stack:
        lines.append("")
        lines.append("<details><summary>Stack (crash first)</summary>")
        lines.append("")
        lines.append("```")
        for entry in stack[:12]:
            lines.append(entry)
        if len(stack) > 12:
            lines.append(f"… {len(stack) - 12} more frames")
        lines.append("```")
        lines.append("</details>")

    try:
        fp = fingerprint(flight_path)
        lines.append("")
        lines.append(f"**Fingerprint** `{fp}` — same id ⇒ same bug (dedup across runs).")
    except Exception:
        pass

    if repro_hint:
        lines.append("")
        lines.append("<sub>Reproduce locally: download the `.flight` artifact, then "
                     "`python -m flight repro " + name + "` (or `flight inspect`, `flight explain`).</sub>")

    return "\n".join(lines)


def _md_escape(s: str) -> str:
    return s.replace("\n", " ").replace("|", "\\|").strip()
