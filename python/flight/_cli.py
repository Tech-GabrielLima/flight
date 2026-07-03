"""The `flight` command line: `run` a script under recording, `inspect` a file.

    python -m flight run myscript.py --script-args
    python -m flight inspect crash.flight
"""

from __future__ import annotations

import argparse
import datetime as _dt
import runpy
import sys
from pathlib import Path

from . import __version__, install, read


def _cmd_run(args: argparse.Namespace) -> int:
    """Run a script with Flight recording installed, like `python script.py`."""
    install(output_dir=Path(args.output_dir) if args.output_dir else Path.cwd())

    script = args.script
    # Make the script see a normal argv and a normal sys.path[0].
    sys.argv = [script, *args.script_args]
    sys.path.insert(0, str(Path(script).resolve().parent))
    try:
        runpy.run_path(script, run_name="__main__")
        return 0
    except SystemExit as e:
        return int(e.code) if isinstance(e.code, int) else (0 if e.code is None else 1)
    # Any other exception propagates to sys.excepthook, which Flight has
    # wrapped to write the .flight — so we let it through.


def _fmt_time(unix_ms: int) -> str:
    if not unix_ms:
        return "unknown"
    return _dt.datetime.fromtimestamp(unix_ms / 1000).isoformat(timespec="seconds")


def _cmd_inspect(args: argparse.Namespace) -> int:
    """Print a human summary of a `.flight` file."""
    f = read(args.file)
    status = "PARTIAL" if f.partial else "complete"
    idx = "index" if f.used_index else "linear-scan"
    print(f"flight file : {f.path}")
    print(f"format      : v{f.format_version}  ({status}, {idx})")
    print(f"written by  : flight {f.flight_version} at {_fmt_time(f.created_unix_ms)}")
    print(f"blocks      : {', '.join(f.blocks) or '(none)'}")
    if f.meta:
        print("environment :")
        print(f"    python   {f.meta.get('python_version', '?')}")
        print(f"    platform {f.meta.get('platform', '?')}")
        argv = f.meta.get("argv", [])
        print(f"    argv     {' '.join(argv) if argv else '(none)'}")
        print(f"    cwd      {f.meta.get('cwd', '?')}")

    if f.exceptions:
        print("exception   :")
        for i, (exc_type, message, relation) in enumerate(f.exceptions):
            prefix = "    " if i == 0 else f"    ({relation}) "
            print(f"{prefix}{exc_type}: {message}")

    if f.has_crash:
        _print_frames(f, show_locals=not args.no_locals, max_locals=args.max_locals)

    if f.has_mutations:
        print(f"mutations   : {f.mutation_count}  (scope recording — see `flight timeline`)")

    if f.has_nondet:
        tape = f.tape()
        sources = ", ".join(f"{s}×{n}" for s, n in sorted(tape.sources().items()))
        print(f"non-det     : {f.nondet_count} recorded  ({sources})")
        print("              (deterministically replayable — flight.replay(path, fn))")

    wrapped = "  (ring wrapped — older events dropped)" if f.wrapped else ""
    print(f"events      : {f.event_count} across {f.code_count} code objects{wrapped}")
    if f.recent_events:
        print("last events (most recent first):")
        for kind, file, line in f.recent_events:
            loc = f"{Path(file).name}:{line}" if file else "?"
            print(f"    {kind:<10} {loc}")
    return 0


# Scalar/leaf kinds: sharing one of these across frames is not the aliasing
# insight (None/True/small ints are singletons), so we don't flag them.
_SCALAR_KINDS = {"none", "bool", "int", "float", "str", "bytes", "redacted", "truncated"}


def _print_frames(f, *, show_locals: bool, max_locals: int) -> None:
    crash = f.crash()
    # Which reference objects appear in more than one frame → aliased (↔).
    counts: dict[int, int] = {}
    for fr in crash.frames:
        for _name, oid in fr.locals:
            node = crash.objects.get(oid)
            if node is not None and node["kind"] not in _SCALAR_KINDS:
                counts[oid] = counts.get(oid, 0) + 1
    print(f"frames      : {len(crash.frames)} (crash first)")
    for i, fr in enumerate(crash.frames):
        where = f"{Path(fr.file).name}:{fr.lineno}"
        print(f"  #{i} {fr.qualname}  ({where})")
        if not show_locals:
            continue
        for name, oid in fr.locals[:max_locals]:
            alias = " ↔" if counts.get(oid, 0) > 1 else ""
            print(f"        {name} = {_clip(crash.render(oid))}{alias}")
        if len(fr.locals) > max_locals:
            print(f"        … {len(fr.locals) - max_locals} more locals")


def _clip(s: str, width: int = 68) -> str:
    s = s.replace("\n", "\\n")
    return s if len(s) <= width else s[:width] + "…"


def _cmd_timeline(args: argparse.Namespace) -> int:
    """Print the mutation timeline of a scope `.flight`."""
    f = read(args.file)
    if not f.has_mutations:
        print("no scope recording in this file (was it written by `with flight.record()`?)")
        return 0
    rec = f.recording()

    if args.var:
        muts = rec.history(args.var)
        print(f"history of local '{args.var}' ({len(muts)} writes):")
    elif args.who:
        muts = rec.who_mutated(args.who)
        print(f"writes to '{args.who}' ({len(muts)} writes):")
    else:
        muts = rec.mutations
        print(f"timeline: {len(muts)} mutations (variables: {', '.join(rec.names()) or '—'})")

    limit = args.limit
    for m in muts[:limit]:
        where = f"{Path(m.file).name}:{m.line}"
        if m.kind == "local":
            target = m.name
        else:
            target = f"{m.name}[{m.key}]" if m.key is not None else m.name
        print(f"  #{m.seq:<5} {where:<22} {m.kind:<6} {target} = {_clip(m.value_repr)}")
    if len(muts) > limit:
        print(f"  … {len(muts) - limit} more (use --limit)")
    return 0


def _cmd_repro(args: argparse.Namespace) -> int:
    """Generate (and verify) a standalone reproduction script from a crash."""
    from ._repro import write_repro

    result = write_repro(args.file, args.output, verify=not args.no_verify)
    if not result.script:
        print(f"cannot build a repro: {result.reason}", file=sys.stderr)
        return 1
    print(f"wrote {result.path}")
    for note in result.notes:
        print(f"  note: {note}")
    if result.approximate:
        print("  (approximate — some values were truncated/redacted or opaque)")
    if result.verified is True:
        print("  ✓ verified: it reproduces the same exception")
    elif result.verified is False:
        print("  ✗ not verified: it did not reproduce (best-effort skeleton)")
    return 0


def _cmd_diff(args: argparse.Namespace) -> int:
    """Compare two `.flight` files and report the first point they diverged."""
    from ._diff import diff_files

    d = diff_files(args.left, args.right)
    print(d.render())
    if d.kind == "incomparable":
        return 2
    return 1 if not d.identical else 0  # like diff(1): nonzero when they differ


def _cmd_debug(args: argparse.Namespace) -> int:
    """Reverse-debug a scope `.flight`. With `--find`, answer a "breakpoint in
    the past" query on the command line; otherwise start a DAP server on stdio
    for VS Code / PyCharm (which get Step-Back / Reverse buttons for free)."""
    f = read(args.file)
    if not f.has_mutations:
        print("no scope recording in this file (was it written by `with flight.record()`?)")
        return 1

    if args.find or args.list:
        from ._timetravel import TimeTravel

        tt = TimeTravel(f.recording())
        if args.list:
            for s in tt.steps[: args.limit]:
                print(f"  {s.describe()}")
            if len(tt) > args.limit:
                print(f"  … {len(tt) - args.limit} more (use --limit)")
            return 0
        step = tt.find_first(args.find)
        if step is None:
            print(f"no write ever matched: {args.find}")
            return 1
        print(f"first match: {step.describe()}")
        locs = tt.state()["locals"]
        shown = {k: v for k, v in locs.items() if not v.startswith("<module ")}
        print("  state there: " + (", ".join(f"{k}={v}" for k, v in sorted(shown.items()))))
        return 0

    # DAP server over stdio for an editor.
    from ._dap import DebugAdapter, serve

    adapter = DebugAdapter(path=args.file)
    serve(sys.stdin.buffer, sys.stdout.buffer, adapter)
    return 0


def _cmd_view(args: argparse.Namespace) -> int:
    """Open the TUI viewer on a `.flight` file."""
    try:
        from ._viewer import run as run_viewer
    except ImportError:
        print(
            "the viewer needs Textual — install it with:\n"
            "    pip install 'flight-recorder[viewer]'   (or: pip install textual)",
            file=sys.stderr,
        )
        return 1
    run_viewer(args.file)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="flight",
        description="A flight recorder for Python — record the last moments before a crash.",
    )
    p.add_argument("--version", action="version", version=f"flight {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="run a script under Flight recording")
    run.add_argument("script", help="path to the .py script to run")
    run.add_argument(
        "script_args", nargs=argparse.REMAINDER, help="arguments passed to the script"
    )
    run.add_argument("--output-dir", help="where to write the .flight on crash")
    run.set_defaults(func=_cmd_run)

    ins = sub.add_parser("inspect", help="print a summary of a .flight file")
    ins.add_argument("file", help="path to the .flight file")
    ins.add_argument("--no-locals", action="store_true", help="don't print frame locals")
    ins.add_argument(
        "--max-locals", type=int, default=12, help="max locals shown per frame (default 12)"
    )
    ins.set_defaults(func=_cmd_inspect)

    tl = sub.add_parser("timeline", help="print the mutation timeline of a scope .flight")
    tl.add_argument("file", help="path to the .flight file")
    tl.add_argument("--var", help="show the history of a single local variable")
    tl.add_argument("--who", help="show writes to a watched container/object by name")
    tl.add_argument("--limit", type=int, default=50, help="max mutations to print (default 50)")
    tl.set_defaults(func=_cmd_timeline)

    vw = sub.add_parser("view", help="open the interactive TUI viewer (needs textual)")
    vw.add_argument("file", help="path to the .flight file")
    vw.set_defaults(func=_cmd_view)

    rp = sub.add_parser("repro", help="generate a standalone reproduction script from a crash")
    rp.add_argument("file", help="path to the crash .flight file")
    rp.add_argument("-o", "--output", help="output script path (default repro_bug.py)")
    rp.add_argument("--no-verify", action="store_true", help="don't run it to verify")
    rp.set_defaults(func=_cmd_repro)

    df = sub.add_parser(
        "diff", help="compare two .flight files and report the first divergence"
    )
    df.add_argument("left", help="the first .flight file (e.g. a run that worked)")
    df.add_argument("right", help="the second .flight file (e.g. a run that failed)")
    df.set_defaults(func=_cmd_diff)

    dbg = sub.add_parser(
        "debug", help="reverse-debug a scope .flight (DAP server, or --find a past breakpoint)"
    )
    dbg.add_argument("file", help="path to the scope .flight file")
    dbg.add_argument(
        "--find", help='breakpoint in the past: jump to the first write matching, e.g. "running > 100"'
    )
    dbg.add_argument("--list", action="store_true", help="list the timeline steps and exit")
    dbg.add_argument("--limit", type=int, default=50, help="max steps to list (default 50)")
    dbg.set_defaults(func=_cmd_debug)

    return p


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)
