#!/usr/bin/env bash
set -euo pipefail

REPO="${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
REPORT_DIR="${COVERAGE_REPORT_DIR:-/tmp/unibridge-coverage}"

if command -v rtk >/dev/null 2>&1; then
  RUN=(rtk)
else
  RUN=()
fi

step() {
  printf '\n==> %s\n' "$*"
}

run() {
  "${RUN[@]}" "$@"
}

run mkdir -p "$REPORT_DIR/ui"
run rm -f \
  "$REPORT_DIR/backend.coverage" \
  "$REPORT_DIR/backend.json" \
  "$REPORT_DIR/converter.coverage" \
  "$REPORT_DIR/converter.json" \
  "$REPORT_DIR/ui/coverage-summary.json"

step "Backend coverage"
(
  cd "$REPO/unibridge-service"
  export COVERAGE_FILE="$REPORT_DIR/backend.coverage"
  run python3 -m pytest tests/ -q \
    --cov=app \
    --cov-config=.coveragerc \
    --cov-report=term-missing:skip-covered \
    --cov-report="json:$REPORT_DIR/backend.json"
)

step "LLM converter coverage"
(
  cd "$REPO/llm-converter"
  export COVERAGE_FILE="$REPORT_DIR/converter.coverage"
  run python3 -m pytest tests/ -q \
    --cov=app \
    --cov-config=.coveragerc \
    --cov-report=term-missing:skip-covered \
    --cov-report="json:$REPORT_DIR/converter.json"
)

step "Frontend coverage"
(
  cd "$REPO/unibridge-ui"
  run npm run test:coverage -- \
    --coverage.reporter=text-summary \
    --coverage.reporter=json-summary \
    --coverage.reportsDirectory="$REPORT_DIR/ui"
)

step "Coverage reports written to $REPORT_DIR"
