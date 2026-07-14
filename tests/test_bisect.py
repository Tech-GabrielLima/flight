from __future__ import annotations

import subprocess
import sys

import pytest

import flight
from flight._bisect import (
    bisect_corpus,
    bisect_repro,
    commit_of,
    git_head,
)
from flight._fingerprint import fingerprint


def _git(repo, *args):
    return subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True, check=True
    ).stdout.strip()


def _init_repo(repo):
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t.t")
    _git(repo, "config", "user.name", "t")
    _git(repo, "config", "commit.gpgsign", "false")


def _commit_all(repo, msg):
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", msg)
    return _git(repo, "rev-parse", "HEAD")


_GOOD = "def divide(numbers):\n    if not numbers:\n        return 0.0\n    return sum(numbers) / len(numbers)\n"
_BAD = "def divide(numbers):\n    return sum(numbers) / len(numbers)\n"


def _make_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    (repo / "buggy.py").write_text(_GOOD)
    c1 = _commit_all(repo, "good: guard empty")
    (repo / "buggy.py").write_text(_BAD)
    c2 = _commit_all(repo, "bug: drop the empty guard")
    (repo / "README").write_text("hi\n")
    c3 = _commit_all(repo, "unrelated readme")
    return repo, c1, c2, c3


def _record_crash(repo, out_path, commit):
    import importlib.util

    spec = importlib.util.spec_from_file_location("buggy", str(repo / "buggy.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    flight.install(commit=commit)
    try:
        mod.divide([])
    except ZeroDivisionError:
        flight.capture(path=str(out_path))
    finally:
        flight.uninstall()
    return str(out_path)


def test_commit_is_stamped_and_read_back(tmp_path):
    repo, c1, c2, c3 = _make_repo(tmp_path)
    p = _record_crash(repo, tmp_path / "c.flight", commit=c3)
    assert commit_of(flight.read(p)) == c3


def test_install_true_autodetects_head(tmp_path):
    repo, c1, c2, c3 = _make_repo(tmp_path)
    assert git_head(repo) == c3


def test_no_commit_stamped_by_default(tmp_path):
    def boom():
        return 1 / 0

    flight.install()
    try:
        boom()
    except ZeroDivisionError:
        p = str(tmp_path / "plain.flight")
        flight.capture(path=p)
    finally:
        flight.uninstall()
    assert commit_of(flight.read(p)) is None


def test_passive_reports_earliest_commit(tmp_path):
    repo, c1, c2, c3 = _make_repo(tmp_path)
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    p2 = _record_crash(repo, corpus / "a.flight", commit=c2)
    _record_crash(repo, corpus / "b.flight", commit=c3)
    fp = fingerprint(p2)
    result = bisect_corpus(corpus, fp, repo=repo)
    assert result.found
    assert result.commit == c2
    assert result.count == 2


def test_passive_prefix_fingerprint(tmp_path):
    repo, c1, c2, c3 = _make_repo(tmp_path)
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    p = _record_crash(repo, corpus / "a.flight", commit=c2)
    fp = fingerprint(p)
    result = bisect_corpus(corpus, fp[:6], repo=repo)
    assert result.found and result.commit == c2


def test_passive_no_match(tmp_path):
    repo, c1, c2, c3 = _make_repo(tmp_path)
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    _record_crash(repo, corpus / "a.flight", commit=c2)
    result = bisect_corpus(corpus, "deadbeef", repo=repo)
    assert not result.found


def test_passive_no_commit_stamped(tmp_path):
    repo, c1, c2, c3 = _make_repo(tmp_path)
    corpus = tmp_path / "corpus"
    corpus.mkdir()

    def boom():
        return 1 / 0

    flight.install()
    try:
        boom()
    except ZeroDivisionError:
        flight.capture(path=str(corpus / "a.flight"))
    finally:
        flight.uninstall()
    fp = fingerprint(str(corpus / "a.flight"))
    result = bisect_corpus(corpus, fp, repo=repo)
    assert not result.found
    assert "commit" in result.detail


def test_active_finds_the_culprit(tmp_path):
    repo, c1, c2, c3 = _make_repo(tmp_path)
    p = _record_crash(repo, tmp_path / "c.flight", commit=c3)
    result = bisect_repro(p, good=c1, bad=c3, repo=repo)
    assert result.found, result.detail
    assert result.commit == c2
    assert c2[:7] in result.render()


def test_active_no_range(tmp_path):
    repo, c1, c2, c3 = _make_repo(tmp_path)
    p = _record_crash(repo, tmp_path / "c.flight", commit=c3)
    result = bisect_repro(p, good=c3, bad=c3, repo=repo)
    assert not result.found


def test_cli_bisect_passive(tmp_path):
    repo, c1, c2, c3 = _make_repo(tmp_path)
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    p = _record_crash(repo, corpus / "a.flight", commit=c2)
    fp = fingerprint(p)
    proc = subprocess.run(
        [sys.executable, "-m", "flight", "bisect", str(corpus), "--fingerprint", fp],
        capture_output=True, text=True, cwd=str(repo),
    )
    assert proc.returncode == 0
    assert "first seen" in proc.stdout
