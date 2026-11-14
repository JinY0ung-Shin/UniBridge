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

step "LLM converter tests and coverage"
(
  cd llm-converter
  run pytest tests/ -v --tb=short \
    --cov=app \
    --cov-config=.coveragerc \
    --cov-report=term-missing:skip-covered
)

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
  run rm -f ci-alembic.db
)

step "Backend tests and coverage"
(
  cd unibridge-service
  run pytest tests/ -v --tb=short \
    --cov=app \
    --cov-config=.coveragerc \
    --cov-report=term-missing:skip-covered
)

step "Frontend install/lint/test/build"
(
  cd unibridge-ui
  run npm ci
  run npx eslint . --max-warnings=0
  run npm run test:coverage
  run npx tsc -b
  run npx vite build
)

step "Shell script syntax"
# One file per call: `bash -n a b` checks only a (b becomes its $1).
for script in backup/backup.sh backup/restore.sh backup/lib/*.sh \
  keycloak/enable-self-registration.sh scripts/*.sh; do
  run bash -n "$script"
done
for script in apisix/docker-entrypoint.sh keycloak/docker-entrypoint.sh \
  unibridge-ui/entrypoint.sh bifrost/entrypoint.sh etcd/init-auth.sh; do
  run sh -n "$script"
done

# The E2E tests run live whenever LLM_API_KEY is set, including from the
# gitignored e2e/.env that e2e/conftest.py loads; without it they skip.
# RUN_LIVE_E2E only names the step.
if [[ "${RUN_LIVE_E2E:-}" == "1" ]]; then
  step "Live E2E"
else
  step "Live E2E skip health (live if LLM_API_KEY is set, e2e/.env included)"
fi
(
  cd e2e
  run pytest -v --tb=short
)

step "Release checks complete"
