"""Reader wrapper and CLI surface."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

import flight
from flight._cli import build_parser, main


def _make_flight(tmp_path: Path) -> Path:
    out = tmp_path / "f.flight"
    flight.install()
    try:
        def loop():
            s = 0
            for i in range(20):
                s += i
            return s

        loop()
        flight.capture(path=out)
    finally:
        flight.uninstall()
    assert out.exists()
    return out


def test_read_returns_flight_dataclass(tmp_path):
    f = flight.read(_make_flight(tmp_path))
    assert isinstance(f, flight.Flight)
    assert f.is_complete
    assert f.format_version == 1
    assert "META" in f.blocks and "EVENT_RING" in f.blocks


def test_truncated_file_reads_as_partial_or_errors_cleanly(tmp_path):
    path = _make_flight(tmp_path)
    data = path.read_bytes()
    # Cut off the footer and part of the last block.
    truncated = tmp_path / "cut.flight"
    truncated.write_bytes(data[: len(data) // 2])
    try:
        f = flight.read(truncated)
    except ValueError:
        # Cutting inside the header is a clean hard error — acceptable.
        return
    assert f.partial or not f.used_index


def test_not_a_flight_file_raises(tmp_path):
    bogus = tmp_path / "nope.flight"
    bogus.write_bytes(b"not a flight file at all")
    with pytest.raises(ValueError):
        flight.read(bogus)


def test_cli_inspect_prints_summary(tmp_path, capsys):
    path = _make_flight(tmp_path)
    rc = main(["inspect", str(path)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "flight file" in out
    assert "META" in out
    assert "events" in out


def test_cli_parser_requires_subcommand():
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args([])


def test_cli_run_executes_and_returns_exit_code(tmp_path):
    script = tmp_path / "ok.py"
    script.write_text("import sys\nprint('hello')\nsys.exit(3)\n")
    proc = subprocess.run(
        [sys.executable, "-m", "flight", "run", str(script)],
        capture_output=True,
        text=True,
    )
    assert "hello" in proc.stdout
    assert proc.returncode == 3
