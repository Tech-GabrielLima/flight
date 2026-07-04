#!/usr/bin/env bash
# Measure Python test coverage — including code that runs in child processes
# (the `python -m flight run` wrapper, the pytest plugin, the crash daemon).
#
# coverage.py only sees the process it starts, so subprocess coverage needs a
# startup hook: a .pth in site-packages that calls coverage.process_startup(),
# active only while COVERAGE_PROCESS_START is set. This script installs that
# hook, runs the suite, combines the per-process data, reports, and removes the
# hook again — leaving the venv untouched.
#
# Install the tooling first: pip install coverage cryptography  (or: pip install
# -e '.[dev]'). Without `cryptography` the AES-GCM branch of _crypto is skipped.
set -euo pipefail
cd "$(dirname "$0")/.."

PY="${PYTHON:-.venv/bin/python}"
SP="$("$PY" -c 'import site; print(site.getsitepackages()[0])')"
HOOK="$SP/zzz_flight_cov_subprocess.pth"
export COVERAGE_FILE="$PWD/.coverage"
export COVERAGE_PROCESS_START="$PWD/.coveragerc"

cleanup() { rm -f "$HOOK"; }
trap cleanup EXIT

echo "import coverage; coverage.process_startup()" > "$HOOK"

"$PY" -m coverage erase
"$PY" -m coverage run --source=flight -m pytest tests/ -q
"$PY" -m coverage combine
"$PY" -m coverage report
