#!/usr/bin/env bash
# Re-run the same commands as .github/workflows/ci.yml, sequentially.
# Useful for measuring the local CI-equivalent wall-clock time.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PYTHON="${PYTHON:-.venv/bin/python}"
NPM="${NPM:-npm}"

stage() {
  local name="$1"
  shift
  echo ""
  echo "=== ${name} ==="
  "$@"
}

stage "ruff" "$PYTHON" -m ruff check python
stage "python tests + coverage" "$PYTHON" -m pytest python/tests -q \
  --cov=homelink --cov-report=term-missing --cov-fail-under=80

stage "api build" bash -c "cd api && ${NPM} run build"
stage "api tests + coverage" bash -c "cd api && ${NPM} run coverage"

stage "dashboard build" bash -c "cd dashboard && ${NPM} run build"
stage "dashboard unit tests" bash -c "cd dashboard && ${NPM} test"

stage "playwright e2e" bash -c "cd dashboard && npx playwright test"

stage "benchmark" "$PYTHON" scripts/bench.py

echo ""
echo "CI-equivalent local run complete."
