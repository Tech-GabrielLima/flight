from __future__ import annotations

import random
import subprocess
import sys

import flight
from flight._diff import diff_html


def _record_tape(path, seed, n=5):
    def work():
        random.seed(seed)
        return [random.randint(1, 6) for _ in range(n)]

    with flight.deterministic(str(path)):
        work()
    return str(path)


def test_diff_html_marks_divergence(tmp_path):
    a = _record_tape(tmp_path / "a.flight", seed=1)
    b = _record_tape(tmp_path / "b.flight", seed=2)
    html = diff_html(a, b)
    assert "<!doctype html>" in html
    assert "flight diff" in html
    assert "diverged at step" in html
    assert "diverge" in html
    assert "a.flight" in html and "b.flight" in html


def test_diff_html_identical(tmp_path):
    a = _record_tape(tmp_path / "a.flight", seed=7)
    b = _record_tape(tmp_path / "b.flight", seed=7)
    html = diff_html(a, b)
    assert "identical on the" in html


def test_diff_html_is_self_contained(tmp_path):
    a = _record_tape(tmp_path / "a.flight", seed=1)
    b = _record_tape(tmp_path / "b.flight", seed=2)
    html = diff_html(a, b)
    assert "http://" not in html and "https://" not in html
    assert "<script" not in html


def test_cli_diff_html(tmp_path):
    a = _record_tape(tmp_path / "a.flight", seed=1)
    b = _record_tape(tmp_path / "b.flight", seed=2)
    out = tmp_path / "diff.html"
    proc = subprocess.run(
        [sys.executable, "-m", "flight", "diff", a, b, "--html", "-o", str(out)],
        capture_output=True, text=True,
    )
    assert proc.returncode == 1
    assert out.exists()
    assert "flight diff" in out.read_text()
