from __future__ import annotations

import argparse
import datetime as _dt
import runpy
import sys
from pathlib import Path

from . import __version__, install, read


def _cmd_run(args: argparse.Namespace) -> int:
    install(
        output_dir=Path(args.output_dir) if args.output_dir else Path.cwd(),
        record_lines=args.lines,
        overhead_slo=args.slo,
        daemon=args.daemon,
    )
    if args.correlate:
        from . import correlate

        correlate(root=True)

    script = args.script
    sys.argv = [script, *args.script_args]
    sys.path.insert(0, str(Path(script).resolve().parent))
    try:
        runpy.run_path(script, run_name="__main__")
        return 0
    except SystemExit as e:
        return int(e.code) if isinstance(e.code, int) else (0 if e.code is None else 1)


def _fmt_time(unix_ms: int) -> str:
    if not unix_ms:
        return "unknown"
    return _dt.datetime.fromtimestamp(unix_ms / 1000).isoformat(timespec="seconds")


def _cmd_inspect(args: argparse.Namespace) -> int:
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


_SCALAR_KINDS = {"none", "bool", "int", "float", "str", "bytes", "redacted", "truncated"}


def _print_frames(f, *, show_locals: bool, max_locals: int) -> None:
    crash = f.crash()
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
    from ._repro import write_repro

    result = write_repro(
        args.file, args.output, verify=not args.no_verify, pytest=args.pytest
    )
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


def _cmd_why(args: argparse.Namespace) -> int:
    from ._slice import backward_slice

    f = read(args.file)
    if not f.has_crash:
        print("no crash frames in this file (nothing to slice)", file=sys.stderr)
        return 1
    sl = backward_slice(f, frame=args.frame, var=args.var, max_hops=args.max_hops)
    print(sl.render())
    return 0 if sl.hops else 1


def _cmd_explain(args: argparse.Namespace) -> int:
    from ._explain import explain

    result = explain(args.file, use_llm=args.llm)
    if args.prompt:
        print(result.prompt)
    else:
        print(result.render())
    return 0


def _cmd_generalize(args: argparse.Namespace) -> int:
    from ._generalize import generalize

    g = generalize(args.file)
    if args.hypothesis:
        text = g.as_hypothesis()
        if args.output:
            Path(args.output).write_text(text)
            print(f"wrote {args.output}")
        else:
            print(text)
        return 0 if g.reproduced else 1
    print(g.render())
    if args.property:
        prop = g.as_property()
        print(f"\ncandidate property: {prop}" if prop else "\n(no single-value property found)")
    return 0 if g.reproduced else 1


def _cmd_fix(args: argparse.Namespace) -> int:
    from ._agent import VERIFIED, fix

    result = fix(args.file, max_tries=args.max_tries, use_llm=args.llm)
    print(result.report())
    if result.patch and args.output and result.status == VERIFIED:
        Path(args.output).write_text(result.patch)
        print(f"\npatch saved → {args.output}")
    return 0 if result.verified else 1


def _cmd_fingerprint(args: argparse.Namespace) -> int:
    from ._fingerprint import fingerprint

    f = read(args.file)
    if not f.has_crash:
        print("no crash in this file")
        return 1
    print(fingerprint(args.file))
    return 0


def _cmd_bisect(args: argparse.Namespace) -> int:
    from ._bisect import bisect_corpus, bisect_repro

    if args.repro:
        if not (args.good and args.bad):
            print("active bisect needs --good and --bad refs", file=sys.stderr)
            return 2
        result = bisect_repro(
            args.repro, args.good, args.bad, build_cmd=args.build, timeout=args.timeout
        )
    else:
        if not args.dir or not args.fingerprint:
            print("passive bisect needs a directory and --fingerprint", file=sys.stderr)
            return 2
        result = bisect_corpus(args.dir, args.fingerprint)
    print(result.render())
    return 0 if result.found else 1


def _cmd_diff(args: argparse.Namespace) -> int:
    from ._diff import diff_files

    if getattr(args, "html", False):
        from ._diff import diff_html

        html = diff_html(args.left, args.right)
        if getattr(args, "output", None):
            Path(args.output).write_text(html)
            print(f"wrote {args.output}")
        else:
            print(html)
        d = diff_files(args.left, args.right)
        return 1 if not d.identical else 0

    d = diff_files(args.left, args.right)
    print(d.render())
    if d.kind == "incomparable":
        return 2
    return 1 if not d.identical else 0


def _cmd_debug(args: argparse.Namespace) -> int:
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

    from ._dap import DebugAdapter, serve

    adapter = DebugAdapter(path=args.file)
    serve(sys.stdin.buffer, sys.stdout.buffer, adapter)
    return 0


def _cmd_trace(args: argparse.Namespace) -> int:
    from ._correlation import trace_graph

    paths: list[Path] = []
    for arg in args.paths:
        p = Path(arg)
        if p.is_dir():
            paths.extend(sorted(p.glob("*.flight")))
        else:
            paths.append(p)
    flights = []
    for p in paths:
        try:
            flights.append(read(p))
        except Exception:
            continue
    groups = trace_graph(flights)
    if not groups:
        print("no correlated .flight files found (none carry a trace context)")
        return 1
    for trace_id, nodes in sorted(groups.items()):
        print(f"trace {trace_id}  ({len(nodes)} service{'s' if len(nodes) != 1 else ''})")
        for node in nodes:
            headline = ""
            fl = read(node.path)
            if fl.exceptions:
                exc_type, message, _rel = fl.exceptions[0]
                headline = f"  — {exc_type}: {_clip(message, 48)}"
            print(f"    [{node.service}] {Path(node.path).name}{headline}")
            for lnk in node.context.links:
                print(f"        ↳ links to {lnk.render()}")
    return 0


def _cmd_ci(args: argparse.Namespace) -> int:
    from ._ci import render_comment

    target = Path(args.file)
    if target.is_dir():
        crashes = sorted(target.glob("*.flight"), key=lambda p: p.stat().st_mtime, reverse=True)
        picked = next((p for p in crashes if read(p).has_crash), None)
        if picked is None:
            print("no crash .flight found in that directory", file=sys.stderr)
            return 1
        target = picked
    md = render_comment(target)
    if args.output:
        Path(args.output).write_text(md + "\n")
    else:
        print(md)
    return 0


def _passphrase(args) -> str:
    import os

    if getattr(args, "passphrase", None):
        return args.passphrase
    env = os.environ.get("FLIGHT_PASSPHRASE")
    if env:
        return env
    import getpass

    return getpass.getpass("passphrase: ")


def _cmd_encrypt(args: argparse.Namespace) -> int:
    from ._crypto import CryptoError, encrypt_file

    try:
        out = encrypt_file(args.file, _passphrase(args), args.output)
    except CryptoError as e:
        print(str(e), file=sys.stderr)
        return 1
    print(f"encrypted → {out}")
    return 0


def _cmd_decrypt(args: argparse.Namespace) -> int:
    from ._crypto import CryptoError, decrypt_file

    try:
        out = decrypt_file(args.file, _passphrase(args), args.output)
    except CryptoError as e:
        print(str(e), file=sys.stderr)
        return 1
    print(f"decrypted → {out}")
    return 0


def _cmd_serve(args: argparse.Namespace) -> int:
    from ._fleet import FleetIndex, serve

    if args.ingest:
        idx = FleetIndex(args.store, index=args.index)
        n = 0
        for p in sorted(Path(args.ingest).glob("*.flight")):
            if idx.ingest_path(p) is not None:
                n += 1
        print(f"ingested {n} crash .flight file(s) into {args.store}")
        return 0
    serve(args.store, host=args.host, port=args.port, index=args.index)
    return 0


def _cmd_view(args: argparse.Namespace) -> int:
    if getattr(args, "serve", False):
        from ._whatif import serve_whatif

        serve_whatif(args.file, host=getattr(args, "host", "127.0.0.1"), port=getattr(args, "port", 8070))
        return 0
    try:
        from ._viewer import run as run_viewer
    except ImportError:
        print(
            "the viewer needs Textual — install it with:\n"
            "    pip install 'pyflight[viewer]'   (or: pip install textual)",
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
    run.add_argument("--lines", action="store_true", help="record per-line events (finest, costliest)")
    run.add_argument(
        "--slo", type=float, metavar="FRAC",
        help="adaptive overhead governor: keep recording overhead under FRAC (e.g. 0.03)",
    )
    run.add_argument(
        "--daemon", action="store_true",
        help="run the supervisor so a black box survives an uncatchable death (SIGKILL/OOM)",
    )
    run.add_argument(
        "--correlate", action="store_true",
        help="stamp a distributed-trace context (from $TRACEPARENT, else a fresh root)",
    )
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

    sv = sub.add_parser("serve", help="fleet mode: collector + index + dashboard over a store")
    sv.add_argument("store", help="directory to hold the index + ingested blobs")
    sv.add_argument("--host", default="127.0.0.1", help="bind host (default 127.0.0.1)")
    sv.add_argument("--port", type=int, default=8080, help="bind port (default 8080)")
    sv.add_argument("--index", help="index DSN (default sqlite in the store dir)")
    sv.add_argument("--ingest", metavar="DIR", help="ingest a directory of .flight files and exit")
    sv.set_defaults(func=_cmd_serve)

    vw = sub.add_parser("view", help="open the TUI viewer, or --serve a browser what-if console")
    vw.add_argument("file", help="path to the .flight file")
    vw.add_argument("--serve", action="store_true", help="serve a browser what-if console instead of the TUI")
    vw.add_argument("--host", default="127.0.0.1", help="bind host for --serve (default 127.0.0.1)")
    vw.add_argument("--port", type=int, default=8070, help="bind port for --serve (default 8070)")
    vw.set_defaults(func=_cmd_view)

    rp = sub.add_parser("repro", help="generate a standalone reproduction script from a crash")
    rp.add_argument("file", help="path to the crash .flight file")
    rp.add_argument("-o", "--output", help="output script path (default repro_bug.py)")
    rp.add_argument("--no-verify", action="store_true", help="don't run it to verify")
    rp.add_argument(
        "--pytest", action="store_true",
        help="emit a committable pytest regression test (test_repro.py)",
    )
    rp.set_defaults(func=_cmd_repro)

    wy = sub.add_parser(
        "why", help="backward slice: why is a value what it is? (writes + aliasings)"
    )
    wy.add_argument("file", help="path to the crash .flight file")
    wy.add_argument("--frame", type=int, default=0, help="frame index to slice from (default 0)")
    wy.add_argument("--var", required=True, help="the local variable to explain")
    wy.add_argument("--max-hops", type=int, default=32, help="max slice hops (default 32)")
    wy.set_defaults(func=_cmd_why)

    ex = sub.add_parser("explain", help="root-cause a crash .flight (heuristics; optional LLM)")
    ex.add_argument("file", help="path to the crash .flight file")
    ex.add_argument("--prompt", action="store_true", help="print the LLM-ready prompt only")
    ex.add_argument("--llm", action="store_true", help="call a configured LLM provider")
    ex.set_defaults(func=_cmd_explain)

    gn = sub.add_parser(
        "generalize", help="find the boundary at which a recorded value flips the failure"
    )
    gn.add_argument("file", help="path to a deterministic crash .flight file")
    gn.add_argument("--property", action="store_true", help="also print the candidate guard")
    gn.add_argument("--hypothesis", action="store_true", help="emit a Hypothesis property-test scaffold")
    gn.add_argument("-o", "--output", help="write the scaffold here (with --hypothesis)")
    gn.set_defaults(func=_cmd_generalize)

    fp = sub.add_parser("fingerprint", help="print a crash's dedup fingerprint (frame + state)")
    fp.add_argument("file", help="path to the crash .flight file")
    fp.set_defaults(func=_cmd_fingerprint)

    fx = sub.add_parser("fix", help="propose and verify a patch that removes the crash")
    fx.add_argument("file", help="path to the crash .flight file")
    fx.add_argument("--llm", action="store_true", help="use a configured LLM provider")
    fx.add_argument("--max-tries", type=int, default=3, help="max patch attempts (default 3)")
    fx.add_argument("-o", "--output", help="write the verified patch here (default fix.patch)")
    fx.set_defaults(func=_cmd_fix)

    bs = sub.add_parser(
        "bisect", help="find the commit that introduced a bug (passive corpus or active repro)"
    )
    bs.add_argument("dir", nargs="?", help="passive: directory of .flight files to group")
    bs.add_argument("--fingerprint", help="passive: the crash fingerprint to date")
    bs.add_argument("--repro", metavar="FILE", help="active: a crash .flight to replay per commit")
    bs.add_argument("--good", help="active: a ref known to be good (bug absent)")
    bs.add_argument("--bad", help="active: a ref known to be bad (bug present, default HEAD)")
    bs.add_argument("--build", help="active: shell command to build at each commit (optional)")
    bs.add_argument("--timeout", type=int, default=60, help="active: per-commit timeout seconds")
    bs.set_defaults(func=_cmd_bisect)

    df = sub.add_parser(
        "diff", help="compare two .flight files and report the first divergence"
    )
    df.add_argument("left", help="the first .flight file (e.g. a run that worked)")
    df.add_argument("right", help="the second .flight file (e.g. a run that failed)")
    df.add_argument("--html", action="store_true", help="render a self-contained side-by-side diff page")
    df.add_argument("-o", "--output", help="write the HTML here (with --html; default stdout)")
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

    tr = sub.add_parser(
        "trace", help="group .flight files by trace id → the cross-service crash graph"
    )
    tr.add_argument("paths", nargs="+", help="`.flight` files or directories to scan")
    tr.set_defaults(func=_cmd_trace)

    ci = sub.add_parser("ci", help="render a Markdown root-cause comment for a crash (for CI)")
    ci.add_argument("file", help="a crash .flight file, or a directory to pick the newest from")
    ci.add_argument("-o", "--output", help="write the Markdown here (default: stdout)")
    ci.set_defaults(func=_cmd_ci)

    enc = sub.add_parser("encrypt", help="encrypt a .flight at rest (AES-256-GCM; [crypto] extra)")
    enc.add_argument("file", help="path to the .flight file")
    enc.add_argument("-o", "--output", help="output path (default <file>.enc)")
    enc.add_argument("--passphrase", help="passphrase (else $FLIGHT_PASSPHRASE, else prompt)")
    enc.set_defaults(func=_cmd_encrypt)

    dec = sub.add_parser("decrypt", help="decrypt a Flight envelope from `flight encrypt`")
    dec.add_argument("file", help="path to the .enc file")
    dec.add_argument("-o", "--output", help="output path (default: strip .enc)")
    dec.add_argument("--passphrase", help="passphrase (else $FLIGHT_PASSPHRASE, else prompt)")
    dec.set_defaults(func=_cmd_decrypt)

    return p


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)
