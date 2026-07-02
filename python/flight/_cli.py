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
    wrapped = "  (ring wrapped — older events dropped)" if f.wrapped else ""
    print(f"events      : {f.event_count} across {f.code_count} code objects{wrapped}")
    if f.recent_events:
        print("last events (most recent first):")
        for kind, file, line in f.recent_events:
            loc = f"{Path(file).name}:{line}" if file else "?"
            print(f"    {kind:<10} {loc}")
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
    ins.set_defaults(func=_cmd_inspect)

    return p


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)
