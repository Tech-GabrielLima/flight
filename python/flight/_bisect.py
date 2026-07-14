from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from ._read import read
from ._repro import _PREAMBLE, _Reconstructor

_SRC_COMMIT = "flight.commit"

_OK = "FLIGHT_BISECT_OK"
_NOEXC = "FLIGHT_BISECT_NOEXC"
_UNRESOLVED = "FLIGHT_BISECT_UNRESOLVED"


def _git(repo, *args, timeout: int = 30) -> Optional[str]:
    try:
        proc = subprocess.run(
            ["git", *args], cwd=str(repo), capture_output=True, text=True, timeout=timeout
        )
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip()


def git_head(cwd=None) -> Optional[str]:
    return _git(cwd or os.getcwd(), "rev-parse", "HEAD")


def _repo_root(cwd=None) -> Optional[str]:
    return _git(cwd or os.getcwd(), "rev-parse", "--show-toplevel")


def commit_of(fl) -> Optional[str]:
    c = fl.meta.get("commit") if getattr(fl, "meta", None) else None
    if c:
        return c
    if not getattr(fl, "has_nondet", False):
        return None
    try:
        for _seq, src, _tag, payload in fl.tape().rows():
            if src == _SRC_COMMIT:
                return payload
    except Exception:
        return None
    return None


def _commit_meta(repo, sha: str) -> str:
    out = _git(repo, "show", "-s", "--format=%h\t%s\t%cs", sha)
    if not out:
        return sha[:12]
    short, subject, date = (out.split("\t") + ["", "", ""])[:3]
    return f'{short} "{subject}" ({date})'


def _topo_order(repo) -> list[str]:
    out = _git(repo, "rev-list", "--all", "--topo-order", "--reverse")
    return out.splitlines() if out else []


def _commits_between(repo, good: str, bad: str) -> list[str]:
    out = _git(repo, "rev-list", "--topo-order", "--reverse", f"{good}..{bad}")
    return out.splitlines() if out else []


@dataclass
class BisectResult:

    mode: str
    found: bool
    commit: Optional[str] = None
    detail: str = ""
    count: int = 0
    tested: int = 0
    fingerprint: Optional[str] = None
    commits: list[str] = field(default_factory=list)

    def render(self) -> str:
        if self.mode == "passive":
            if not self.found:
                return self.detail or "no matching recordings found"
            return (
                f"first seen: commit {self.detail}\n"
                f"   {self.count} recording(s) share this fingerprint, "
                f"all at or after this commit"
            )
        if not self.found:
            return self.detail or "no culprit found (the bug predates the range)"
        return f"culprit: {self.detail}  ({self.tested} commit(s) tested)"


def bisect_corpus(directory, fingerprint: str, *, repo=None) -> BisectResult:
    from ._fingerprint import fingerprint as fp_of

    d = Path(directory)
    repo = repo or _repo_root(d) or _repo_root()
    order = {sha: i for i, sha in enumerate(_topo_order(repo))} if repo else {}

    matches: list[tuple[str, str]] = []
    for path in sorted(d.glob("*.flight")):
        try:
            fl = read(path)
        except Exception:
            continue
        if not fl.has_crash:
            continue
        try:
            fp = fp_of(path)
        except Exception:
            continue
        if not (fp == fingerprint or fp.startswith(fingerprint)):
            continue
        matches.append((str(path), commit_of(fl) or ""))

    if not matches:
        return BisectResult(
            "passive", False, fingerprint=fingerprint,
            detail=f"no recording in {directory} matched fingerprint {fingerprint}",
        )

    commits = [c for _p, c in matches if c]
    if not commits:
        return BisectResult(
            "passive", False, fingerprint=fingerprint, count=len(matches),
            detail=(
                f"{len(matches)} recording(s) match, but none carry a commit — "
                "record with `flight.install(commit=True)` to enable dating"
            ),
        )

    earliest = min(commits, key=lambda c: order.get(c, len(order) + 1))
    known = earliest in order if order else False
    detail = _commit_meta(repo, earliest) if (repo and known) else earliest[:12]
    return BisectResult(
        "passive", True, commit=earliest, detail=detail,
        count=len(matches), fingerprint=fingerprint,
        commits=sorted(set(commits)),
    )


@dataclass
class _Harness:
    script: str
    file: str
    exc_type: str


def _build_harness(flight_path) -> Optional[_Harness]:
    fl = read(flight_path)
    if not fl.has_crash:
        return None
    crash = fl.crash()
    if not crash.frames:
        return None
    frame = crash.frames[0]

    rec = _Reconstructor(crash.objects)
    ref_lines: list[str] = []
    local_names: list[str] = []
    for name, oid in frame.locals:
        if name.startswith("__") and name.endswith("__"):
            continue
        expr = rec.build(oid)
        ref_lines.append(f"{name}_ref = {expr}")
        local_names.append(name)
    build_lines = rec.lines + ref_lines

    exc_type = crash.exceptions[0][0].split(".")[-1] if crash.exceptions else "Exception"
    tape_json = fl.tape_json() if fl.has_nondet else None
    script = _render_harness(
        file=frame.file, qualname=frame.qualname, exc_type=exc_type,
        build_lines=build_lines, local_names=local_names, tape_json=tape_json,
    )
    return _Harness(script=script, file=frame.file, exc_type=exc_type)


def _render_harness(*, file, qualname, exc_type, build_lines, local_names, tape_json) -> str:
    p = [_PREAMBLE, "", "import os as _os", "import importlib.util as _ilu", ""]
    p.append(f"_REC_FILE = {json.dumps(file)}")
    p.append("_FILE = _os.environ.get('FLIGHT_BISECT_MODULE') or _REC_FILE")
    p.append("_spec = _ilu.spec_from_file_location('_flight_target', _FILE)")
    p.append("if _spec is None or _spec.loader is None:")
    p.append(f"    print({_UNRESOLVED!r}, _FILE); _sys.exit(3)")
    p.append("_mod = _ilu.module_from_spec(_spec)")
    p.append("try:")
    p.append("    _spec.loader.exec_module(_mod)")
    p.append("except BaseException:")
    p.append("    pass  # top-level may fail; the target fn may already be defined")
    p.append("")
    p.append("# --- reconstructed crash-frame state ---")
    p.extend(build_lines)
    p.append("_locals = {")
    for name in local_names:
        p.append(f"    {name!r}: {name}_ref,")
    p.append("}")
    p.append("")
    if tape_json is not None:
        p.append("try:")
        p.append("    import flight as _flight")
        p.append(f"    _tape = _flight.Tape.from_json({tape_json!r})")
        p.append("except Exception:")
        p.append("    _tape = None")
        p.append("")
    p.append(f"_qualname = {json.dumps(qualname)}")
    p.append("_fn = _mod")
    p.append("for _part in _qualname.split('.'):")
    p.append("    if _part == '<locals>':")
    p.append("        _fn = None; break")
    p.append("    _fn = getattr(_fn, _part, None)")
    p.append("    if _fn is None: break")
    p.append("if _fn is None or not callable(_fn):")
    p.append(f"    print({_UNRESOLVED!r}, _qualname); _sys.exit(3)")
    p.append("_params = [pp.name for pp in _inspect.signature(_fn).parameters.values()")
    p.append("           if pp.kind in (pp.POSITIONAL_OR_KEYWORD, pp.KEYWORD_ONLY)]")
    p.append("_args = {k: _locals[k] for k in _params if k in _locals}")
    p.append(f"_expected = {json.dumps(exc_type)}")
    p.append("")
    p.append("def _invoke():")
    p.append("    return _fn(**_args)")
    p.append("")
    p.append("def _attempt():")
    if tape_json is not None:
        p.append("    if _tape is None:")
        p.append("        _invoke(); return")
        p.append("    for _ in range(len(_tape) + 1):")
        p.append("        try:")
        p.append("            _flight.replay_tape(_tape, _invoke)")
        p.append("        except _flight.ReplayDivergence:")
        p.append("            return")
    else:
        p.append("    _invoke()")
    p.append("")
    p.append("try:")
    p.append("    _attempt()")
    p.append("except BaseException as _e:")
    p.append("    if type(_e).__name__ == _expected:")
    p.append(f"        print({_OK!r}, _expected); _sys.exit(0)")
    p.append(f"    print({_NOEXC!r}, type(_e).__name__); _sys.exit(1)")
    p.append(f"print({_NOEXC!r}); _sys.exit(1)")
    return "\n".join(p) + "\n"


@contextmanager
def _worktree(repo, commit: str):
    tmp = tempfile.mkdtemp(prefix="flight-bisect-")
    wt = os.path.join(tmp, "wt")
    added = _git(repo, "worktree", "add", "--detach", wt, commit) is not None
    try:
        yield wt if added else None
    finally:
        if added:
            _git(repo, "worktree", "remove", "--force", wt)
        try:
            os.rmdir(tmp)
        except OSError:
            pass


def _rel_in_repo(repo, file: str) -> Optional[str]:
    try:
        return os.path.relpath(os.path.realpath(file), os.path.realpath(repo))
    except Exception:
        return None


class _Skip(Exception):
    pass


def _test_commit(repo, commit: str, harness: _Harness, rel: str, build_cmd, timeout: int) -> bool:
    with _worktree(repo, commit) as wt:
        if wt is None:
            raise _Skip(commit)
        module_path = os.path.join(wt, rel) if rel else harness.file
        if not os.path.exists(module_path):
            raise _Skip(commit)
        if build_cmd:
            try:
                b = subprocess.run(build_cmd, cwd=wt, shell=True, capture_output=True, timeout=timeout)
                if b.returncode != 0:
                    raise _Skip(commit)
            except _Skip:
                raise
            except Exception:
                raise _Skip(commit)
        script = os.path.join(wt, "_flight_bisect_harness.py")
        Path(script).write_text(harness.script)
        env = dict(os.environ, FLIGHT_BISECT_MODULE=module_path)
        try:
            proc = subprocess.run(
                [sys.executable, script], cwd=wt, env=env,
                capture_output=True, text=True, timeout=timeout,
            )
        except Exception:
            raise _Skip(commit)
        if _UNRESOLVED in proc.stdout:
            raise _Skip(commit)
        return _OK in proc.stdout


def _bisect_search(commits: list[str], test: Callable[[str], bool]) -> tuple[Optional[str], int]:
    lo, hi = 0, len(commits) - 1
    culprit: Optional[str] = None
    tested = 0
    skipped: set[int] = set()
    while lo <= hi:
        mid = (lo + hi) // 2
        idx = _nearest_testable(mid, lo, hi, skipped)
        if idx is None:
            break
        try:
            tested += 1
            reproduces = test(commits[idx])
        except _Skip:
            skipped.add(idx)
            continue
        if reproduces:
            culprit = commits[idx]
            hi = idx - 1
        else:
            lo = idx + 1
    return culprit, tested


def _nearest_testable(mid: int, lo: int, hi: int, skipped: set[int]) -> Optional[int]:
    for delta in range(0, hi - lo + 1):
        for cand in (mid - delta, mid + delta):
            if lo <= cand <= hi and cand not in skipped:
                return cand
    return None


def bisect_repro(
    flight_path, good: str, bad: str, *, repo=None, build_cmd=None, timeout: int = 60
) -> BisectResult:
    repo = repo or _repo_root()
    if repo is None:
        return BisectResult("active", False, detail="not inside a git repository")
    harness = _build_harness(flight_path)
    if harness is None:
        return BisectResult("active", False, detail="cannot build a harness (no reconstructable crash)")
    rel = _rel_in_repo(repo, harness.file)
    commits = _commits_between(repo, good, bad)
    if not commits:
        return BisectResult("active", False, detail=f"no commits in range {good}..{bad}")

    culprit, tested = _bisect_search(
        commits, lambda c: _test_commit(repo, c, harness, rel, build_cmd, timeout)
    )
    if culprit is None:
        return BisectResult(
            "active", False, tested=tested,
            detail=f"no culprit in {good}..{bad} (the bug may predate it)",
            commits=commits,
        )
    return BisectResult(
        "active", True, commit=culprit, tested=tested,
        detail=_commit_meta(repo, culprit), commits=commits,
    )
