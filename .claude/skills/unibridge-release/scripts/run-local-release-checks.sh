#!/usr/bin/env bash
set -euo pipefail

REPO="${1:-/home/jinyoung/UniBridge}"
cd "$REPO"

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

step "Repository diff whitespace"
run git diff --check

step "LLM converter syntax"
run python3 -m compileall -q llm-converter/app llm-converter/tests

step "LLM converter lint"
run python3 -m ruff check llm-converter/app llm-converter/tests

step "LLM converter tests"
run pytest llm-converter/tests -v --tb=short

step "Backend lint"
(
  cd unibridge-service
  run ruff check app/
)

step "Backend migration check"
(
  cd unibridge-service
  export META_DB_URL=sqlite+aiosqlite:///./ci-alembic.db
  run alembic -c alembic.ini upgrade head
  run alembic -c alembic.ini check
  rm -f ci-alembic.db
)

step "Backend tests"
(
  cd unibridge-service
  run pytest tests/ -v --tb=short
)

step "Frontend install/lint/test/build"
(
  cd unibridge-ui
  run npm ci
  run npx eslint . --max-warnings=0
  run npx vitest run
  run npx tsc -b
  run npx vite build
)

step "Shell script syntax"
run bash -n backup/backup.sh backup/restore.sh backup/lib/*.sh keycloak/enable-self-registration.sh
run sh -n apisix/docker-entrypoint.sh keycloak/docker-entrypoint.sh unibridge-ui/entrypoint.sh

if [[ "${RUN_LIVE_E2E:-}" == "1" ]]; then
  step "Live E2E"
  (
    cd e2e
    run pytest -v
  )
else
  step "Live E2E skip health"
  (
    cd e2e
    run pytest -v --tb=short
  )
fi

step "Release checks complete"
