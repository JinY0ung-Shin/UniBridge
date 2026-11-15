# unibridge-service (FastAPI backend)

See the repo-root `CLAUDE.md` for cross-service context and the boot/migration gotchas.
Python 3.12, SQLAlchemy async + Alembic. Meta store = Postgres when deployed, SQLite locally and in tests.

## Layout
- `app/main.py`        — FastAPI app + lifespan: validate settings, `init_db()`, load DB/S3/NAS
  connections, re-provision the fixed APISIX system routes (`_provision_system_routes` from
  the `ServiceRoute` table, `_provision_llm_routes`, the llm-bi-not-found 404, node-path-prefix
  rule; boot retries go through `_retry_at_boot`), sync API-key route restrictions, start
  the alert checker and the periodic loops. `_BACKGROUND_TASKS` is the shutdown order
  (the alert checker stops last). Mounts all routers.
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
  Shared helpers live there too, not in a router: `apisix_client.py` (admin API plus
  `route_paths`, `is_not_found`/`get_resource_or_none`, `split_host_port`, and
  `upstream_scheme`/`node_host`/`upstream_host_header` to probe a node the way APISIX does),
  `apisix_system_resources.py` (system ids and the fixed `ServiceRoute` table),
  `meta_store.py` (`advisory_section`: a critical section across both blue/green colors;
  `load_config_model`/`save_config_model`: settings kept as JSON in `system_config`),
  `capped_read.py` (`read_capped`: a body read whole up to a size limit),
  `active_color.py` (`run_periodic`: a background loop on the active color) and
  `prometheus_client.py` (`http_error`, `map_by_label`). UTC helpers: `app/db_types.py`.
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
- Shared helpers go in `tests/helpers.py` (`run_lifespan`, `sessions`, `insert_api_key`/
  `create_api_key_via_admin`, `make_role_token`, `http_status_error`, `FakeApisix`, …),
  `tests/mcp_helpers.py` (MCP RPC helpers, `FakeGateway`) and `tests/http_mock.py` (the
  `http_mock` fixture on `httpx.MockTransport`; pytest-httpx is gone). Test modules never
  import from each other.
- No ruff config file → ruff defaults.
