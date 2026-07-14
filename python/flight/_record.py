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

    def __init__(self, obj: Any, label: str):
        self.obj = obj
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

    def __init__(self, session, path, scrubber: Scrubber):
        self.session = session
        self.path = path
        self.owner = _thread_id()
        self.scrubber = scrubber
        self.max_mutations = session.config.capture_max_mutations
        self.mutations: list = []
        self.truncated = False
        self._seq = 0
        self._locals: dict[int, dict[str, int]] = {}
        self._prev_line: dict[int, int] = {}
        self.watches: list[_Watch] = []
        self.path_written: Optional[str] = None


    def watch(self, obj: Any, name: Optional[str] = None) -> Any:
        label = name or _default_label(obj)
        self.watches.append(_Watch(obj, label))
        return obj


    def capture_line(self, code, line: int, frame) -> None:
        fid = id(frame)
        attr = self._prev_line.get(fid, line)
        self._diff_frame(code, attr, frame, fid)
        self._prev_line[fid] = line

    def capture_return(self, code, frame) -> None:
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
                    continue
                vid = id(value)
                if prev.get(name) != vid:
                    prev[name] = vid
                    self._emit("local", name, None, value, code, attr_line, fid)
        for w in self.watches:
            w.diff(self, code, attr_line, fid)


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
        return False


def record(path=None, *, watch=()):
    return _Recording(path, watch)


def watch(obj: Any, name: Optional[str] = None) -> Any:
    from . import _install

    session = _install._active
    if session is not None:
        scope = session._current_scope()
        if scope is not None:
            return scope.watch(obj, name)
    return obj


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
