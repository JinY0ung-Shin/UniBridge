# unibridge-service (FastAPI backend)

See the repo-root `CLAUDE.md` for cross-service context and the boot/migration gotchas.
Python 3.12, SQLAlchemy async + Alembic. Meta store = Postgres when deployed, SQLite locally and in tests.

## Layout
- `app/main.py`        — FastAPI app + lifespan: validate settings, `init_db()`, load DB/S3/NAS
  connections, re-provision the fixed APISIX system routes (`_provision_llm_routes`,
  `_retire_llm_bi_routes`, node-path-prefix rule), sync API-key route restrictions, start
  the alert checker and retention/key-expiry loops. Mounts all routers.
- `app/auth.py`        — JWT verify (Keycloak RS256 / dev HS256), `ALL_PERMISSIONS`, RBAC
  + 60s permission cache, API-key auth.
- `app/config.py`      — `Settings` (pydantic-settings) + `validate_settings()` fail-fast.
- `app/models.py`      — SQLAlchemy ORM (`Base`). `app/schemas.py` — Pydantic request/response.
- `app/database.py`    — engine, `get_db()`, `init_db()` (auto `alembic upgrade head`).
- `app/routers/`       — one module per API area (`ls app/routers`).
- `app/services/`      — APISIX client, connection/query executors, alert pipeline,
  S3/NAS managers, SQL/SPARQL validators, audit, openapi export,
  `consumer_restrictions.py` (reconciles APISIX consumer-restriction whitelists
  with the API-key store — this is where key expiry is enforced at the gateway), etc.
- `app/middleware/rate_limiter.py` — per-user rate + concurrency limits.

## Migrations
Auto-applied at boot. To add one after changing `models.py`:
```bash
alembic -c alembic.ini revision --autogenerate -m "describe change"   # env.py: compare_type=True
alembic -c alembic.ini upgrade head
alembic -c alembic.ini check                                          # must be clean (release gate)
```
Migrations are numbered `NNNN_*.py` in `alembic/versions/`.
They must run on both dialects, but the release gate upgrades only SQLite, so a Postgres-only
failure first shows at deploy (boot runs `upgrade head`). Column/constraint changes go through
`op.batch_alter_table` for SQLite, as most existing migrations do.
A boot that finds the database past its own head (the old blue/green color restarting) skips the
upgrade and the role seed instead of crash-looping, so migrations must stay expand/contract.
Changing the system roles (`ALL_PERMISSIONS` or `database._seed_role_definitions`) needs a
`ROLE_SEED_VERSION` bump: an image with a lower number leaves newer roles alone, and
`tests/test_seed_roles.py` pins the number to a digest of the definitions.

## Tests (`pytest tests/`)
- `conftest.py` sets env (in-memory SQLite, dev JWT) **before** importing the app.
- Key fixtures: `engine`/`db_session` (bare schema), `seeded_db` (admin/user roles seeded),
  `app`/`client` (ASGI `AsyncClient` on the seeded DB), `admin_token`/`user_token` + `auth_header()`.
  A test that changes roles/permissions itself must `await invalidate_permission_cache()`
  (as `querier_token` does); the fixtures only invalidate after their own seeding.
- No ruff config file → ruff defaults.
