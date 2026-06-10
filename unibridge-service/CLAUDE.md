# unibridge-service (FastAPI backend)

See the repo-root `CLAUDE.md` for cross-service context and the boot/migration gotchas.
Python 3.12, SQLAlchemy async + Alembic, meta store = SQLite.

## Layout
- `app/main.py`        — FastAPI app + lifespan (validates settings, runs `init_db()`,
  loads DB/S3/NAS connections, starts alert checker). Mounts all routers.
- `app/auth.py`        — JWT verify (Keycloak RS256 / dev HS256), `ALL_PERMISSIONS`, RBAC
  + 60s permission cache, API-key auth.
- `app/config.py`      — `Settings` (pydantic-settings) + `validate_settings()` fail-fast.
- `app/models.py`      — SQLAlchemy ORM (`Base`). `app/schemas.py` — Pydantic request/response.
- `app/database.py`    — engine, `get_db()`, `init_db()` (auto `alembic upgrade head`).
- `app/routers/`       — admin, alerts, api_keys, gateway, nas, query, query_history, roles, s3, users.
- `app/services/`      — APISIX client, connection/query executors, alert pipeline,
  S3/NAS managers, SQL/SPARQL validators, audit, openapi export, etc.
- `app/middleware/rate_limiter.py` — per-user rate + concurrency limits.

## Migrations
Auto-applied at boot. To add one after changing `models.py`:
```bash
alembic -c alembic.ini revision --autogenerate -m "describe change"   # env.py: compare_type=True
alembic -c alembic.ini upgrade head
alembic -c alembic.ini check                                          # must be clean (CI gate)
```
Migrations are numbered `NNNN_*.py` in `alembic/versions/`.

## Tests (`pytest tests/`)
- `conftest.py` sets env (in-memory SQLite, dev JWT) **before** importing the app.
- Key fixtures: `engine`/`db_session` (bare schema), `seeded_db` (admin/user roles seeded),
  plus an ASGI `AsyncClient`. After mutating roles/permissions in a test, the seeded fixture
  already calls `invalidate_permission_cache()`.
- Lint scope is `app/` only (`ruff check app/`); no ruff config file → defaults.
