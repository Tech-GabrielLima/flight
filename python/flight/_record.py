"""Phase 2 — time-travel of scope: `with flight.record()`.

Inside the scope we record **every state write** as a MUTATION, so afterwards
you can ask "what was `x` at step t?" and "who mutated this dict?" — the
event-sourcing model applied to program memory (VISION.md §10).

**How writes are captured (the honest engineering choice).** The guide (§3.2)
weighs three options: bytecode rewriting (exact but fragile across CPython
releases), the `INSTRUCTION` event (robust but can't read the value just pushed
on the stack), and container proxies (which break `type(x) is dict`). This
implementation takes the robust, version-independent path: on each `LINE` event
inside the scope we

  * diff the frame's locals against the previous line → **local (re)binds**, and
  * diff each `watch()`-ed object against its last snapshot → **container/attr
    writes** — non-invasively, without ever subclassing or breaking `type()`.

Both are line-granular. That is coarser than per-instruction capture but it is
robust, needs no bytecode surgery, and delivers the headline Phase-2 powers:
a per-variable value history, a reconstructable timeline, and "who mutated
this". Finer granularity via native bytecode instrumentation is a documented
future step (TECHNICAL.md §3.2, option A).

Recording is opt-in and scope-delimited, so its cost is only paid around the
code you are actually investigating (P2).
"""

from __future__ import annotations

import hashlib
import os
import platform
import sys
import time
from typing import Any, Optional

from . import _core
from ._scrub import DEFAULT_PATTERNS, Scrubber
from ._serialize import describe_shallow

_REDACTED_VALUE = ("redacted", "<redacted>", None, None)
_DELETED_VALUE = ("deleted", "<deleted>", None, None)


class _Watch:
    """Tracks writes to one container/object by snapshot-diffing (option C, but
    non-invasive: we never replace the object, so `type()` is untouched)."""

    def __init__(self, obj: Any, label: str):
        self.obj = obj  # strong ref for the scope's lifetime
        self.label = label
        if isinstance(obj, dict):
            self.kind = "dict"
        elif isinstance(obj, list):
            self.kind = "list"
        else:
            self.kind = "object"
        self.snap = self._snapshot()

    def _snapshot(self):
        obj = self.obj
        try:
            if self.kind == "dict":
                return {k: id(v) for k, v in obj.items()}
            if self.kind == "list":
                return [id(v) for v in obj]
            d = getattr(obj, "__dict__", None)
            return {k: id(v) for k, v in d.items()} if isinstance(d, dict) else {}
        except Exception:
            return {} if self.kind != "list" else []

    def diff(self, scope: "_Scope", code, line: int, frame_id: int) -> None:
        obj = self.obj
        try:
            if self.kind == "dict":
                for k, v in list(obj.items()):
                    if self.snap.get(k) != id(v):
                        scope._emit("item", self.label, str(k), v, code, line, frame_id)
                for k in list(self.snap.keys()):
                    if k not in obj:
                        scope._emit_value("item", self.label, str(k), _DELETED_VALUE, code, line, frame_id)
            elif self.kind == "list":
                for i, v in enumerate(obj):
                    if i >= len(self.snap) or self.snap[i] != id(v):
                        scope._emit("item", self.label, str(i), v, code, line, frame_id)
            else:
                d = getattr(obj, "__dict__", {})
                if isinstance(d, dict):
                    for k, v in list(d.items()):
                        if self.snap.get(k) != id(v):
                            scope._emit("attr", self.label, str(k), v, code, line, frame_id)
            self.snap = self._snapshot()
        except Exception:
            pass


class _Scope:
    """A live `with flight.record()` recording on one thread."""

    def __init__(self, session, path, scrubber: Scrubber):
        self.session = session
        self.path = path
        self.owner = _thread_id()
        self.scrubber = scrubber
        self.max_mutations = session.config.capture_max_mutations
        self.mutations: list = []
        self.truncated = False
        self._seq = 0
        self._locals: dict[int, dict[str, int]] = {}  # id(frame) -> {name: id(value)}
        self._prev_line: dict[int, int] = {}  # id(frame) -> last line event seen
        self.watches: list[_Watch] = []
        self.path_written: Optional[str] = None

    # -- public (via flight.watch / the returned object) -------------------

    def watch(self, obj: Any, name: Optional[str] = None) -> Any:
        """Track writes to `obj` for the rest of the scope. Returns `obj`."""
        label = name or _default_label(obj)
        self.watches.append(_Watch(obj, label))
        return obj

    # -- capture (called from the LINE callback) ---------------------------

    def capture_line(self, code, line: int, frame) -> None:
        # A LINE event fires *before* its line runs, so any change we see now
        # was made by the previous line executed in this frame — attribute it
        # there for an exact line, not one line late.
        fid = id(frame)
        attr = self._prev_line.get(fid, line)
        self._diff_frame(code, attr, frame, fid)
        self._prev_line[fid] = line

    def capture_return(self, code, frame) -> None:
        """Final diff for a frame about to return or unwind.

        Line-diff detects a write on the *next* LINE event in the frame — but a
        function's last statement has no next LINE event, so its write would be
        lost. Diffing once more at PY_RETURN/PY_UNWIND closes that gap; the last
        line executed is `frame.f_lineno`, so attribution stays exact. We then
        drop the frame's bookkeeping (it is exiting)."""
        if _thread_id() != self.owner:
            return
        fid = id(frame)
        try:
            self._diff_frame(code, frame.f_lineno, frame, fid)
        finally:
            self._locals.pop(fid, None)
            self._prev_line.pop(fid, None)

    def _diff_frame(self, code, attr_line: int, frame, fid: int) -> None:
        if _thread_id() != self.owner or self._full():
            return
        try:
            cur = frame.f_locals
        except Exception:
            cur = None
        if isinstance(cur, dict) or hasattr(cur, "items"):
            prev = self._locals.setdefault(fid, {})
            for name, value in list(cur.items()):
                if name.startswith("__") and name.endswith("__"):
                    continue  # skip dunder machinery locals
                vid = id(value)
                if prev.get(name) != vid:
                    prev[name] = vid
                    self._emit("local", name, None, value, code, attr_line, fid)
        for w in self.watches:
            w.diff(self, code, attr_line, fid)

    # -- emission ----------------------------------------------------------

    def _emit(self, kind, name, key, value, code, line, frame_id) -> None:
        scrub_name = key if key is not None else name
        if self.scrubber.should_redact(scrub_name):
            rendered = _REDACTED_VALUE
        else:
            rendered = describe_shallow(value)
        self._emit_value(kind, name, key, rendered, code, line, frame_id)

    def _emit_value(self, kind, name, key, rendered, code, line, frame_id) -> None:
        if self._full():
            return
        self.mutations.append(
            (
                self._seq,
                kind,
                name,
                key,
                rendered,
                code.co_filename,
                code.co_qualname,
                int(line),
                frame_id,
            )
        )
        self._seq += 1

    def _full(self) -> bool:
        if len(self.mutations) >= self.max_mutations:
            self.truncated = True
            return True
        return False

    # -- write on exit -----------------------------------------------------

    def write(self) -> Optional[str]:
        path = self.path
        if path is None:
            path = self.session.config.scope_path(os.getpid(), int(time.time() * 1000))
        files: dict[str, None] = {}
        for m in self.mutations:
            files.setdefault(m[5], None)
        sources = []
        for fn in files:
            text = _read_source(fn)
            if text is not None:
                sha1 = hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()
                sources.append((fn, sha1, text))
        from . import __version__

        try:
            _core.dump_scope(
                str(path),
                platform.python_version(),
                platform.platform(),
                list(sys.argv),
                _cwd(),
                __version__,
                self.mutations,
                sources,
            )
            self.path_written = str(path)
            return str(path)
        except Exception:
            return None


class _Recording:
    """The context manager returned by :func:`record`."""

    def __init__(self, path=None, watch=()):
        self.path = path
        self._watch = list(watch)
        self.scope: Optional[_Scope] = None
        self._auto_installed = False
        self._entry_frame = None

    def __enter__(self) -> _Scope:
        from . import _install

        session = _install._active
        if session is None:
            _install.install()
            session = _install._active
            self._auto_installed = True
        scrubber = Scrubber(DEFAULT_PATTERNS + tuple(session.config.scrub_patterns))
        self.scope = _Scope(session, self.path, scrubber)
        for obj in self._watch:
            self.scope.watch(obj)
        # The frame running the `with` statement: its last block statement has
        # no trailing LINE event, so we diff it once more in __exit__.
        self._entry_frame = sys._getframe(1)
        session._enter_scope(self.scope)
        return self.scope

    def __exit__(self, exc_type, exc, tb) -> bool:
        assert self.scope is not None
        try:
            if self._entry_frame is not None:
                self.scope.capture_line(
                    self._entry_frame.f_code, self._entry_frame.f_lineno, self._entry_frame
                )
        except Exception:
            pass
        try:
            self.scope.session._exit_scope(self.scope)
        except Exception:
            pass
        path = self.scope.write()
        if path is not None:
            note = " (partial)" if self.scope.truncated else ""
            print(f"[flight] recorded scope {path}{note}", file=sys.stderr)
        if self._auto_installed:
            from . import _install

            _install.uninstall()
        return False  # never suppress the user's exception


def record(path=None, *, watch=()):
    """Record every state write within the block into a `.flight`.

        with flight.record() as rec:
            rec.watch(cache)          # track a specific container too
            run_the_suspect_code()
        # -> writes a scope .flight; inspect its timeline with
        #    `python -m flight timeline <file>`

    `path` names the output file (default: a timestamped name); `watch` is an
    optional iterable of objects to track from the start of the scope.
    """
    return _Recording(path, watch)


def watch(obj: Any, name: Optional[str] = None) -> Any:
    """Track writes to `obj` in the currently active scope on this thread.

    A no-op (returns `obj` unchanged) if called outside a `with flight.record()`
    scope, so instrumentation left in code is harmless in production.
    """
    from . import _install

    session = _install._active
    if session is not None:
        scope = session._current_scope()
        if scope is not None:
            return scope.watch(obj, name)
    return obj


# -- helpers ----------------------------------------------------------------


def _thread_id() -> int:
    import threading

    return threading.get_ident()


def _default_label(obj: Any) -> str:
    t = type(obj)
    return f"<{t.__name__} at 0x{id(obj):x}>"


def _read_source(filename: str) -> Optional[str]:
    import linecache

    if not filename or filename.startswith("<"):
        return None
    try:
        lines = linecache.getlines(filename)
    except Exception:
        return None
    return "".join(lines) if lines else None


def _cwd() -> str:
    try:
        return os.getcwd()
    except Exception:
        return ""
