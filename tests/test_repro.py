"""Phase 3, rung 1 — shallow reproduction from a crash .flight."""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

import flight
from flight._repro import build_repro, write_repro


def _record_crash(tmp_path, source: str, name="prog.py") -> Path:
    """Run `source` as a script under `flight run`, return the crash .flight."""
    script = tmp_path / name
    script.write_text(textwrap.dedent(source))
    out_dir = tmp_path / "out"
    out_dir.mkdir(exist_ok=True)
    subprocess.run(
        [sys.executable, "-m", "flight", "run", "--output-dir", str(out_dir), str(script)],
        capture_output=True,
        text=True,
    )
    files = list(out_dir.glob("*.flight"))
    assert files, "no crash file produced"
    return files[0]


def test_repro_verifies_a_keyerror(tmp_path):
    f = _record_crash(
        tmp_path,
        """
        def normalize(record, weights):
            total = 0
            for key, w in weights.items():
                total += record[key] * w
            return total
        def main():
            normalize({"a": 10}, {"a": 1.0, "b": 2.0})
        if __name__ == "__main__":
            main()
        """,
    )
    result = write_repro(f, tmp_path / "repro.py", verify=True)
    assert result.verified is True
    assert result.path.exists()
    # the reconstructed args are in the script
    assert "record" in result.script and "weights" in result.script


def test_repro_reconstructs_nested_containers(tmp_path):
    f = _record_crash(
        tmp_path,
        """
        def crash(config):
            return config["servers"][10]["host"]   # IndexError
        crash({"servers": [{"host": "a"}, {"host": "b"}]})
        """,
    )
    result = build_repro(f)
    # nested list/dict rebuilt
    assert "servers" in result.script
    assert ".append(" in result.script


def test_repro_verified_runs_standalone(tmp_path):
    f = _record_crash(
        tmp_path,
        """
        def divide(numerator, denominator):
            return numerator / denominator
        divide(10, 0)
        """,
    )
    out = tmp_path / "repro.py"
    result = write_repro(f, out, verify=True)
    assert result.verified is True
    # run it ourselves, fresh, to double-check it's truly self-contained
    proc = subprocess.run([sys.executable, str(out)], capture_output=True, text=True)
    assert proc.returncode == 0
    assert "FLIGHT_REPRO_OK" in proc.stdout


def test_repro_handles_cycle_without_crashing_generation(tmp_path):
    f = _record_crash(
        tmp_path,
        """
        def boom(node):
            return node["missing"]
        d = {}
        d["self"] = d          # cycle
        boom(d)
        """,
    )
    result = build_repro(f)
    # generation must succeed and produce a create-then-fill for the cycle
    assert result.script
    assert "_v0 = {}" in result.script


def test_repro_opaque_object_becomes_stub_and_is_approximate(tmp_path):
    f = _record_crash(
        tmp_path,
        """
        class Widget:
            def __init__(self):
                self.size = 3
        def use(w):
            return w.parts[w.size]        # AttributeError: no 'parts'
        use(Widget())
        """,
    )
    result = build_repro(f)
    assert "_Stub(" in result.script
    assert result.approximate  # stub reconstruction is approximate


def test_repro_replays_recorded_nondeterminism(tmp_path):
    # A crash that depends on randomness: the deterministic block records the
    # exact random draw, and the repro replays it — reproducing a flaky bug.
    script = tmp_path / "flaky.py"
    out = tmp_path / "flaky.flight"
    script.write_text(
        textwrap.dedent(
            f"""
            import flight, random
            def process(items):
                i = random.randint(0, len(items))   # off-by-one: can be len(items)
                return items[i]
            def run():
                data = [10, 20, 30]
                with flight.deterministic({str(out)!r}):
                    for _ in range(200):
                        process(data)                # eventually i == 3 -> IndexError
            if __name__ == "__main__":
                run()
            """
        )
    )
    subprocess.run([sys.executable, str(script)], capture_output=True, text=True)
    assert out.exists()

    f = flight.read(out)
    assert f.has_crash and f.has_nondet  # one file carries both

    repro = tmp_path / "repro.py"
    result = write_repro(out, repro, verify=True)
    assert result.verified is True
    assert "replay_tape" in result.script  # the tape is woven into the repro
    # deterministic: it reproduces every single time
    for _ in range(3):
        proc = subprocess.run([sys.executable, str(repro)], capture_output=True, text=True)
        assert "FLIGHT_REPRO_OK" in proc.stdout


def test_repro_of_nested_function_does_not_crash_generation(tmp_path):
    # A crash inside a closure has a "<locals>" qualname that can't be resolved
    # by import; generation must still succeed (best-effort skeleton).
    f = _record_crash(
        tmp_path,
        """
        def make():
            def inner(x):
                return x[5]      # IndexError inside a closure
            return inner
        make()([1, 2])
        """,
    )
    result = build_repro(f)
    assert result.script  # generation succeeded
    assert "<locals>" in result.script  # the unresolved qualname is emitted


def test_repro_reports_no_crash_file(tmp_path):
    # a ring-only file has no frames
    out = tmp_path / "ring.flight"
    flight.install()
    (lambda: sum(range(5)))()
    flight.dump(out)
    flight.uninstall()
    result = build_repro(out)
    assert result.script == ""
    assert "no crash" in result.reason
