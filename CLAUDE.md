# UniBridge (API Hub)

Internal API/DB gateway platform: register databases (Postgres/MSSQL/ClickHouse/Neo4j),
run SQL through one endpoint, manage APISIX routes, proxy LLMs via LiteLLM, browse
S3/NAS — all behind Keycloak OIDC + RBAC + API keys. See `README.md` for deployment.

## Repo layout (monorepo, 3 code services + infra config)
- `unibridge-service/` — FastAPI backend (Python 3.12). `app/{routers,services,middleware}`,
  SQLAlchemy async + Alembic. Meta store = SQLite (`data/meta.db`) by default. See its CLAUDE.md.
- `unibridge-ui/`      — React 19 + TS + Vite, served by nginx. TanStack Query,
  react-router 7, keycloak-js, i18next, recharts. See its CLAUDE.md.
- `llm-converter/`     — FastAPI sidecar: translates Anthropic `/v1/messages` and
  OpenAI `/v1/responses` → chat/completions for LiteLLM. See its CLAUDE.md.
- `e2e/`               — live end-to-end tests (skipped unless `LLM_API_KEY` set).
- `apisix/ keycloak/ litellm/ prometheus/` — infra config. `docker-compose.yml` — full stack.

## Commands
Backend (`cd unibridge-service`):
```bash
ruff check app/                              # lint (CI gate; scope is app/ only)
pytest tests/ -v --tb=short                  # tests (in-memory SQLite)
alembic -c alembic.ini upgrade head          # apply migrations
alembic -c alembic.ini check                 # CI fails if models drifted from migrations
uvicorn app.main:app --reload                # run locally (listens on :8000 — UI dev proxy expects this)
```
Frontend (`cd unibridge-ui`):
```bash
npm ci                                       # install from package-lock
npm run dev                                  # dev server
npm run lint                                 # eslint . -- CI treats warnings as failures
npm run test                                 # vitest run
npm run build                                # tsc -b && vite build
```
Converter (`cd llm-converter`): `pytest`
E2E (`cd e2e`): `pytest -v`   (needs `LLM_API_KEY`; see e2e/README.md)
Full stack: `docker compose up -d`

Release gate: use the Codex `$unibridge-release` skill. It runs local frontend
lint/test/build, backend ruff + alembic upgrade/check + pytest, converter pytest,
shell syntax checks, and e2e skip-health before tagging/publishing. Live E2E
requires `RUN_LIVE_E2E=1` plus `LLM_API_KEY`.

## Gotchas
- **Migrations auto-apply at boot**: `app.main` lifespan → `init_db()` → `alembic upgrade head`.
  Adding/changing a model REQUIRES a new `alembic/versions/NNNN_*.py`, or `alembic check`
  (CI) fails and real DBs miss the column. Tests skip this (in-memory SQLite → `create_all`
  + stamp head), so a missing migration passes tests but breaks deploys.
- **Test env is set in `tests/conftest.py` before app import** (in-memory SQLite, dev JWT,
  `ENABLE_DEV_TOKEN_ENDPOINT=true`). Don't rely on a real `.env` in tests.
- **Fail-fast secrets**: ENCRYPTION_KEY, APISIX_ADMIN_KEY, KC_*, LITELLM_* are required at
  boot (compose `:?` + `validate_settings()`). Keycloak/CORS URLs auto-derive from `HOST_IP`;
  Keycloak URLs derive only when `KEYCLOAK_JWT_AUDIENCE` is set (empty = dev HS256 mode).
- **RBAC**: flat string permissions in `app/auth.py::ALL_PERMISSIONS`; roles→permissions with
  a 60s in-process cache (`invalidate_permission_cache()` after changes).
- **APISIX consumer-restriction is preserved** on route updates for fixed route IDs
  (`query-api`, `llm-proxy`, `s3-api`, `llm-messages`, `llm-responses`, `nas-api`) via
  `main.py::_preserve_consumer_restriction` — don't clobber it.
- **Request routing**: UI nginx → `/_api/*` = unibridge-service, `/api/*` = APISIX gateway.
- **TZ=UTC everywhere**; timestamps stored UTC (see `scripts/backfill_utc_timestamps.py`).
- `.omc/`, `docs/superpowers/`, `certs/`, `unibridge-service/data/` are gitignored — tooling
  artifacts / local state, not app code.
