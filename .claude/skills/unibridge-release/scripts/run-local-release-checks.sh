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

step "Backend migration backward-compatibility scan"
(
  # Blue/green runs the OLD color against the NEW schema (the new color applies
  # 'alembic upgrade head' at boot while the old one still serves traffic). A
  # contract operation (drop column/table, SET NOT NULL, rename) in a migration
  # that ships in the SAME release as the code removing those fields breaks the
  # still-live old color. Expand/contract must span releases. This is a warning,
  # not a hard gate, because the split is sometimes intentional across versions.
  base_ref="${RELEASE_DIFF_BASE:-origin/main}"
  if git rev-parse --verify --quiet "$base_ref" >/dev/null; then
    new_migrations="$(git diff --name-only --diff-filter=A "$base_ref"...HEAD \
      -- unibridge-service/alembic/versions/ 2>/dev/null || true)"
  else
    new_migrations=""
    printf '   (skipped: base ref %s not found; set RELEASE_DIFF_BASE)\n' "$base_ref"
  fi
  risky=0
  for f in $new_migrations; do
    [ -f "$f" ] || continue
    # Only inspect the upgrade() body; downgrade() is never run by the deploy.
    up_body="$(awk '/^def upgrade\(/{p=1} /^def downgrade\(/{p=0} p' "$f")"
    hits="$(printf '%s\n' "$up_body" | grep -nE 'drop_column|drop_table|drop_constraint|\.alter_column\(.*nullable=False|rename' || true)"
    if [ -n "$hits" ]; then
      risky=1
      printf '   ⚠ %s contains potentially backward-INCOMPATIBLE DDL:\n' "$f"
      printf '%s\n' "$hits" | sed 's/^/       /'
    fi
  done
  if [ "$risky" = 1 ]; then
    printf '   Confirm the OLD code still runs against this schema, or split into expand (this release) + contract (a later release).\n'
  else
    printf '   No backward-incompatible DDL detected in new migrations.\n'
  fi
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
