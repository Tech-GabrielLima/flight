from __future__ import annotations

import hashlib
import linecache
import os
import platform
import sys
import time
from pathlib import Path
from typing import Optional

from . import _core
from ._config import Config
from ._scrub import DEFAULT_PATTERNS, Scrubber
from ._serialize import GraphSerializer


def write_crash_flight(exc_type, exc_value, exc_tb, config: Config, path=None, correlation=None) -> Optional[Path]:
    try:
        return _capture(exc_value, exc_tb, config, path, correlation=correlation)
    except BaseException:
        return None


def capture(config: Config, path=None, correlation=None) -> Optional[Path]:
    exc_type, exc_value, exc_tb = sys.exc_info()
    if exc_value is not None and exc_tb is not None:
        return write_crash_flight(exc_type, exc_value, exc_tb, config, path, correlation=correlation)
    from ._install import dump

    return dump(path, config=config)


def build_payload(exc_value, exc_tb, config: Config):
    frames_raw = []
    tb = exc_tb
    while tb is not None:
        frames_raw.append((tb.tb_frame, tb.tb_lineno))
        tb = tb.tb_next
    frames_raw.reverse()

    scrubber = Scrubber(DEFAULT_PATTERNS + tuple(config.scrub_patterns))
    graph = GraphSerializer(
        deadline_ms=config.capture_deadline_ms,
        max_bytes=config.capture_max_bytes,
        max_str=config.max_str,
        max_container=config.max_container,
        max_depth=config.max_depth,
        repr_limit=config.repr_limit,
        scrubber=scrubber,
    )

    frame_tuples = []
    filenames: list[str] = []
    for frame, lineno in frames_raw:
        code = frame.f_code
        if code.co_filename not in filenames:
            filenames.append(code.co_filename)
        try:
            local_items = list(dict(frame.f_locals).items())
        except Exception:
            local_items = []
        local_ids = [(str(name), graph.add_local(str(name), value)) for name, value in local_items]
        frame_tuples.append(
            (code.co_filename, code.co_qualname, int(lineno), int(code.co_firstlineno), local_ids)
        )

    objects = graph.run()

    source_tuples = []
    for filename in filenames:
        text = _read_source(filename)
        if text is not None:
            sha1 = hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()
            source_tuples.append((filename, sha1, text))

    exc_tuples = _exception_chain(exc_value)
    return source_tuples, exc_tuples, frame_tuples, objects


def _capture(exc_value, exc_tb, config: Config, path, nondet=None, correlation=None) -> Optional[Path]:
    source_tuples, exc_tuples, frame_tuples, objects = build_payload(exc_value, exc_tb, config)

    from . import __version__

    if path is None:
        path = config.crash_path(pid=os.getpid(), when_ms=int(time.time() * 1000))
    nondet_all = list(nondet or [])
    ctx = correlation if correlation is not None else getattr(config, "correlation", None)
    if ctx is not None:
        try:
            nondet_all.extend(ctx.to_nondet())
        except Exception:
            pass
    commit = getattr(config, "commit", None)
    if isinstance(commit, str) and commit:
        try:
            from ._bisect import _SRC_COMMIT

            nondet_all.append((len(nondet_all), _SRC_COMMIT, "w", commit))
        except Exception:
            pass
    _core.dump_crash(
        str(path),
        platform.python_version(),
        platform.platform(),
        list(sys.argv),
        _cwd(),
        __version__,
        source_tuples,
        exc_tuples,
        frame_tuples,
        objects,
        nondet_all,
    )
    return path


def _read_source(filename: str) -> Optional[str]:
    if not filename or filename.startswith("<"):
        return None
    try:
        lines = linecache.getlines(filename)
    except Exception:
        return None
    return "".join(lines) if lines else None


def _exception_chain(exc) -> list[tuple[str, str, str]]:
    out: list[tuple[str, str, str]] = []
    seen: set[int] = set()
    cur = exc
    relation = "head"
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        out.append((_exc_type_name(cur), _exc_message(cur), relation))
        if cur.__cause__ is not None:
            cur, relation = cur.__cause__, "cause"
        elif not getattr(cur, "__suppress_context__", False) and cur.__context__ is not None:
            cur, relation = cur.__context__, "context"
        else:
            break
    return out


def _exc_type_name(exc) -> str:
    t = type(exc)
    mod = getattr(t, "__module__", "")
    return t.__qualname__ if mod in ("builtins", "") else f"{mod}.{t.__qualname__}"


def _exc_message(exc) -> str:
    try:
        return str(exc)
    except BaseException as e:
        return f"<str failed: {type(e).__name__}>"


def _cwd() -> str:
    try:
        return os.getcwd()
    except Exception:
        return ""
