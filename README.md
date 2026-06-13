# API Hub (UniBridge)

Internal API/DB gateway platform. Register multiple databases (PostgreSQL, MSSQL, ClickHouse), execute SQL through a single endpoint, manage API routes via APISIX, and control access with RBAC + API keys.

## Architecture

```
Browser ──HTTPS──> unibridge-ui (nginx)
                       │
                       ├── /_api/* ──> unibridge-service (FastAPI)
                       └── /api/*  ──> apisix (API Gateway)
                                          │
                                          ├── /api/query/*     → Registered databases (Postgres, MSSQL, ClickHouse)
                                          ├── /api/llm/v1/messages
                                          │                  → llm-converter → LiteLLM
                                          ├── /api/llm/v1/responses
                                          │                  → llm-converter → LiteLLM
                                          ├── /api/llm/*       → LiteLLM (LLM proxy)
                                          ├── /api/llm-admin/* → LiteLLM Admin UI/API
                                          ├── /api/s3/*        → S3 connections
                                          ├── /api/nas/*       → Mounted NAS/local files
                                          └── Custom upstream services

Keycloak   ── OIDC auth
Prometheus ── APISIX/LiteLLM/FastAPI metrics + DB TCP probes
LiteLLM    ── Unified LLM proxy (+ Postgres)
```

**Services (10):** etcd, APISIX, Keycloak + Postgres, unibridge-service, Prometheus, Blackbox Exporter, LiteLLM + Postgres, unibridge-ui

## Prerequisites

- Docker & Docker Compose v2
- TLS certificate pair (`tls.crt`, `tls.key`)

## Quick Start

### 1. Clone & configure

```bash
git clone https://github.com/JinY0ung-Shin/UniBridge.git
cd UniBridge
cp .env.example .env
```

### 2. Edit `.env`

**Must set before first boot:**

`.env.example` intentionally leaves deployment secrets blank. After `cp .env.example .env`, fill in the values below before running `docker compose up`.

| Variable | Description |
|----------|-------------|
| `ENCRYPTION_KEY` | Fail-fast secret used to encrypt stored database credentials. Generate with `python -c "import secrets; print(secrets.token_urlsafe(32))"` |
| `KC_ADMIN_PASSWORD` | Fail-fast secret for the Keycloak admin console |
| `KC_DB_PASSWORD` | Fail-fast secret for the Keycloak database |
| `APISIX_ADMIN_KEY` | Fail-fast secret for the APISIX admin API |
| `KEYCLOAK_SERVICE_CLIENT_SECRET` | Fail-fast shared secret used by Keycloak and unibridge-service |
| `LITELLM_DB_PASSWORD` | Fail-fast secret for the LiteLLM database |
| `LITELLM_MASTER_KEY` | Fail-fast secret for LiteLLM admin/API access |
| `ETCD_ROOT_PASSWORD` | Set this unless `ETCD_ALLOW_NONE_AUTH=yes` for dev-only etcd without auth |
| `HOST_IP` | Server IP or hostname that browsers access (not `localhost` in production) |
| `JWT_SECRET` | Required when not using Keycloak-issued tokens; generate a separate strong value |

**Optional:**

| Variable | Default | Description |
|----------|---------|-------------|
| `UNIBRIDGE_UI_PORT` | 3000 | HTTPS port for the web UI |
| `KEYCLOAK_PORT` | 8443 | Keycloak OIDC port |
| `KEYCLOAK_EXTERNAL_URL` | derived | Browser-facing Keycloak base URL for UI runtime config |
| `KEYCLOAK_DEV_MODE` | false | Set `true` to run Keycloak in dev mode (relaxed security) |
| `ETCD_ALLOW_NONE_AUTH` | no | Set `yes` to disable etcd authentication (dev only) |
| `ENABLE_DEV_TOKEN_ENDPOINT` | false | Set `true` only for local dev |
| `SSL_VERIFY` | true | Set `false` if using self-signed certs |
| `RATE_LIMIT_PER_MINUTE` | 60 | Per-user query rate limit |
| `MAX_CONCURRENT_QUERIES` | 5 | Per-user concurrent query limit |
| `NAS_HOST_PATH` | `/mnt/nas` | Host path bind-mounted read-only into `unibridge-service` for NAS browsing |
| `NAS_CONTAINER_PATH` | `/mnt/nas` | Container path where the NAS host path appears |
| `NAS_ALLOWED_ROOTS` | `NAS_CONTAINER_PATH` | Comma-separated container paths allowed as NAS connection `base_path` roots |
| `S3_OP_TIMEOUT_SECONDS` | 30 | Per-operation timeout for S3-compatible storage calls |

### 3. TLS certificates

Place cert files in `certs/`:

```bash
# Self-signed (dev/test only)
openssl req -x509 -newkey rsa:2048 -nodes \
  -keyout certs/tls.key -out certs/tls.crt \
  -days 365 -subj "/CN=${HOST_IP}"
```

Or copy your real certificates:

```bash
cp /path/to/your/cert.crt certs/tls.crt
cp /path/to/your/cert.key certs/tls.key
```

### 4. Start

```bash
docker compose up -d
```

First boot takes ~2 minutes (Keycloak initialization).

For near-zero-downtime updates with Docker Compose, use the split blue/green
stack instead of recreating the public UI container directly:

```bash
scripts/deploy-bluegreen.sh deploy blue   # first bootstrap
scripts/deploy-bluegreen.sh deploy        # later updates
```

See [`docs/blue-green-deploy.md`](docs/blue-green-deploy.md) for the split
Compose files, volume-name migration notes, rollback, and APISIX promotion
details.

### 5. Access

| Service | URL |
|---------|-----|
| Web UI | `https://<HOST_IP>:<UNIBRIDGE_UI_PORT>` |
| Keycloak Admin | `https://<HOST_IP>:<KEYCLOAK_PORT>/admin` |
| API Gateway | `https://<HOST_IP>:<UNIBRIDGE_UI_PORT>/api/*` |
| LiteLLM | `https://<HOST_IP>:<LITELLM_PORT>` |
| Prometheus | `http://<HOST_IP>:9090` (localhost only) |

Default login: Keycloak admin console (`KC_ADMIN_USER` / `KC_ADMIN_PASSWORD`). No human users are seeded into the `apihub` realm by default. After first boot, sign in to the admin console and create the first `admin` user in the `apihub` realm (assign the `admin` realm role), then manage further users from the UI **Users** page.

### Codex through UniBridge

Codex can use UniBridge through the OpenAI-compatible Responses endpoint exposed at `/api/llm/v1/responses`. The gateway authenticates the caller with APISIX `key-auth`, injects the LiteLLM master key internally, and sends the request through `llm-converter`, which translates Responses API traffic to LiteLLM's `/v1/chat/completions` shape and translates the result back.

Configure Codex in your user-level `~/.codex/config.toml`:

```toml
model_provider = "unibridge"
model = "<LiteLLM model id>"

[model_providers.unibridge]
name = "UniBridge"
base_url = "https://<HOST_IP>:<UNIBRIDGE_UI_PORT>/api/llm/v1"
wire_api = "responses"
env_http_headers = { "apikey" = "UNIBRIDGE_API_KEY" }
stream_idle_timeout_ms = 300000
```

Then export an API key that has LLM access:

```bash
export UNIBRIDGE_API_KEY="<UniBridge API key>"
```

Requirements and behavior:

- Put provider/auth settings in user config (`~/.codex/config.toml`), not project `.codex/config.toml`; Codex ignores provider and auth redirects from project config.
- Grant the API key LLM access. Granting the `llm-proxy` route also whitelists the converter routes `llm-messages` and `llm-responses`.
- Use a certificate Codex trusts. For self-signed dev certificates, install the CA locally or use a trusted certificate for the UniBridge UI endpoint.
- Codex reasoning effort is forwarded as Responses `reasoning.effort`; `llm-converter` maps it to upstream Chat Completions `reasoning_effort`.
- Streaming Responses events include `response.created`, `response.output_text.delta`, function-call argument deltas, terminal `response.completed` / `response.failed`, and monotonic `sequence_number` values.

### NAS mount

UniBridge does not mount SMB/NFS itself. Mount the NAS on the Docker host first, then point `NAS_HOST_PATH` at that mounted directory. Docker Compose exposes it read-only inside `unibridge-service` at `NAS_CONTAINER_PATH`.

```env
NAS_HOST_PATH=/srv/company-nas
NAS_CONTAINER_PATH=/mnt/nas
NAS_ALLOWED_ROOTS=/mnt/nas
```

After changing these values, recreate the service so Docker applies the bind mount:

```bash
docker compose up -d --force-recreate unibridge-service
```

In the UI, add a NAS connection with `base_path` set to `/mnt/nas` or a child directory such as `/mnt/nas/reports`. External API-key access then uses alias-relative paths. The browse APIs are read-only:

```http
GET /api/nas/company-nas/entries?path=reports&limit=100
GET /api/nas/company-nas/entries?path=reports&q=invoice&limit=100
GET /api/nas/company-nas/metadata?path=reports/2026/a.csv
GET /api/nas/company-nas/download?path=reports/2026/a.csv
```

The `q` parameter searches only the immediate `path` directory by case-insensitive file or folder name substring. It is not recursive.

### Self-service registration (approval-gated)

The `apihub` realm has registration enabled (`registrationAllowed: true`), but registration is **approval-gated**: a new person can click **Register** on the Keycloak login page, but the new account receives **no application role** and cannot use the service. They see a "pending approval" screen; the backend rejects role-less tokens with 401. An **admin approves** the account by assigning the `user` role from the UI **Users** page (pending accounts show a *Pending* badge there).

Flow: `register → pending (no role) → admin assigns a role → access`.

> **Security note:** registration is open, so anyone who can reach the Keycloak login page can create a *pending* account. Pending accounts have no access and cannot mint API keys, so the blast radius of mass/bot registration is limited to Keycloak user-row growth (admins simply never approve them). `bruteForceProtected` is enabled (login lockout). For a fully public surface you may still want reCAPTCHA on the registration flow or network-restricting Keycloak, but neither is required for access control here — approval is the gate.

How it works (two realm settings, both applied by the helper):

1. `registrationAllowed = true` — shows the **Register** link.
2. The `user` role is **not** in the `default-roles-apihub` composite — so new users are role-less (pending) until an admin assigns a role. (Default roles only apply at user-creation time; existing users are unaffected.)

`registrationAllowed` ships in `keycloak/realm-export.json`, but that template is only read when Keycloak **first creates** the realm. Run the idempotent helper once on the Docker host to apply it to a running deployment (it also removes `user` from the default roles if present, guaranteeing the approval gate):

```bash
./keycloak/enable-self-registration.sh
# override container/realm if needed:
# KC_CONTAINER=unibridge-keycloak-1 KC_REALM=apihub ./keycloak/enable-self-registration.sh
```

The helper authenticates as the Keycloak master admin (the service account lacks `manage-realm`), errors out clearly on zero/multiple container matches or auth failure (printing an admin-console fallback), and is safe to re-run.

**To disable registration entirely:** set `registrationAllowed=false` (admin console → Realm settings → Login → *User registration*, or `kcadm.sh update realms/apihub -s registrationAllowed=false` inside the Keycloak container as master admin).

## Service Ports (default)

| Port | Service | Binding |
|------|---------|---------|
| 3000 | unibridge-ui (HTTPS) | public |
| 8443 | Keycloak (HTTPS) | public |
| 4000 | LiteLLM (HTTPS) | public |
| 8000 | unibridge-service | localhost only |
| 9180 | APISIX admin | localhost only |
| 9090 | Prometheus | localhost only |

## Local Development (without Docker)

### Backend

```bash
cd unibridge-service
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
pip install pytest pytest-asyncio pytest-cov

# Minimal .env for temporary backend-only dev
# Do not reuse these example values for Docker/production deployments.
export META_DB_URL="sqlite+aiosqlite:///data/meta.db"
export ENCRYPTION_KEY="dev-key-change-in-prod-32chars!"
export JWT_SECRET="dev-jwt-secret"
export ENABLE_DEV_TOKEN_ENDPOINT=true

uvicorn app.main:app --reload --port 8000
```

### Frontend

```bash
cd unibridge-ui
npm ci
npm run dev   # http://localhost:5173, proxies /_api to :8000
```

### Tests

```bash
# Backend
cd unibridge-service && pytest tests/ -v

# Frontend
cd unibridge-ui && npx vitest run
```

## Common Operations

```bash
# Restart all
docker compose restart

# Rebuild after code change
docker compose up -d --build

# View logs
docker compose logs -f unibridge-service
docker compose logs -f keycloak

# Stop
docker compose down

# Stop and remove volumes (DESTROYS DATA)
docker compose down -v
```

Operational defaults in `docker-compose.yml`:

- All services use `restart: unless-stopped`, so containers come back after host/container restarts unless intentionally stopped.
- Docker `json-file` logs rotate at `50m` with `5` retained files per service.
- Each service has an initial `deploy.resources.limits` CPU/memory cap for Docker Compose v2, plus `mem_limit`/`cpus` fallbacks for older Compose compatibility. Treat these as conservative starting values and tune from `docker stats` on the deploy host.
- `unibridge-service` and `unibridge-ui` run with `init: true` for PID 1 signal handling and child process reaping.
- Prometheus scrapes APISIX, LiteLLM, unibridge-service `/metrics`, and Blackbox TCP probes for the Postgres-backed services. Alert rules live under `prometheus/rules/`.

## Backups

Stateful components (etcd, Keycloak DB, LiteLLM DB, unibridge-service meta DB) are backed up by scripts in [`backup/`](./backup/README.md). Snapshots land in `./snapshots/` (gitignored) with 14-day retention, and manifest SHA256s are verified before any destructive restore.

### Deploy-time setup

1. **Pull the latest tree** on the deploy host:
   ```bash
   git pull
   ```
2. **Install host-side prerequisites** (needed by `restore.sh` for manifest verification):
   ```bash
   apt-get install -y jq        # or python3 — either one works
   ```
3. **Schedule the nightly backup** via cron (`crontab -e`):
   ```
   0 3 * * * cd /opt/unibridge && ./backup/backup.sh >> /var/log/unibridge-backup.log 2>&1
   ```
4. **Run a restore drill before relying on it.** Follow the "Full-disaster recovery order" in [`backup/README.md`](./backup/README.md) against a disposable environment, end-to-end at least once. A backup you haven't tested restoring is a wish, not a backup.

Retention, path overrides, and the full restore runbook live in [`backup/README.md`](./backup/README.md).

## Timezone Migration (one-time, after upgrading to the UTC-aware timestamp fix)

The timezone-consistency fix changes the on-disk format of every `DateTime` column in the meta DB. Pre-fix rows were stored at second precision without an offset; post-fix rows use microsecond precision. SQLite compares TEXT columns lexicographically, so the older shorter strings get excluded from boundary `>=` filters until they are normalized.

Run the following sequence on the deploy host **once** when upgrading past this change:

```bash
git pull
docker compose up -d --build unibridge-service unibridge-ui
docker compose exec unibridge-service python -m scripts.backfill_utc_timestamps
```

The third command introspects every `UtcDateTime` column, skips tables that don't yet exist in the DB, and rewrites legacy values to the canonical microsecond form. It is idempotent — re-running it finds 0 rows to update.

> **SQLite-only.** The lexicographic-compare bug fixed by this script is specific to SQLite. PostgreSQL / MSSQL deployments store datetimes as native timestamp types and do not need this step; the script will hard-stop with `RuntimeError` if run against a non-SQLite backend.

## etcd Authentication Migration Guide

etcd는 APISIX의 설정 저장소로, 기본적으로 인증이 활성화되어 있습니다. 기존 환경에서 업그레이드하는 경우 아래 절차를 따라주세요.

### 신규 설치

`.env`에 `ETCD_ROOT_PASSWORD`만 설정하면 자동으로 적용됩니다.

```bash
# .env
ETCD_ROOT_PASSWORD=your-strong-password-here
```

### 기존 환경에서 마이그레이션

기존에 인증 없이 운영하던 etcd 볼륨이 있는 경우, 두 가지 방법 중 택일합니다.

**방법 1: 볼륨 초기화 (권장, 설정 데이터 재생성)**

```bash
# 1. 서비스 중지
docker compose down

# 2. etcd 볼륨 삭제 (APISIX 라우트/업스트림 설정이 초기화됩니다)
docker volume rm unibridge_etcd-data

# 3. .env에 패스워드 설정
#    ETCD_ROOT_PASSWORD=your-strong-password-here

# 4. 재시작 (APISIX 라우트는 unibridge-service 기동 시 자동 재프로비저닝)
docker compose up -d
```

> APISIX 라우트(query-api, llm-proxy, llm-admin)와 업스트림은 `unibridge-service` 시작 시 자동으로 재생성됩니다. 수동으로 추가한 커스텀 라우트/업스트림만 다시 등록하면 됩니다.

**방법 2: 인증 없이 유지 (개발/테스트 전용)**

```bash
# .env
ETCD_ALLOW_NONE_AUTH=yes
# ETCD_ROOT_PASSWORD는 비워두거나 생략
```

## Key Features

- **Multi-DB support** — PostgreSQL, MSSQL, ClickHouse via a single query endpoint
- **LLM Proxy** — Unified LiteLLM gateway with centralized auth, usage metrics, and per-model analytics
- **S3 Connections** — Register S3-compatible storage and browse objects through the gateway
- **Alerts** — Rule-based alerting with webhook delivery and history
- **APISIX Gateway** — Route management, upstream config, API key auth
- **RBAC** — 22 granular permissions, dynamic role management
- **API Keys** — External access with per-database/route restrictions
- **Monitoring** — Prometheus metrics, request trends, latency percentiles
- **User Management** — Keycloak integration, role assignment
- **Audit Logging** — Full query history with filters
- **i18n** — Korean / English
