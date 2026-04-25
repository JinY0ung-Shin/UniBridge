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
                                          ├── /api/llm/*       → LiteLLM (LLM proxy)
                                          ├── /api/llm-admin/* → LiteLLM Admin UI/API
                                          ├── /api/s3/*        → S3 connections
                                          └── Custom upstream services

Keycloak   ── OIDC auth
Prometheus ── APISIX metrics
LiteLLM    ── Unified LLM proxy (+ Postgres)
```

**Services (9):** etcd, APISIX, Keycloak + Postgres, unibridge-service, Prometheus, LiteLLM + Postgres, unibridge-ui

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
| `KEYCLOAK_DEV_MODE` | false | Set `true` to run Keycloak in dev mode (relaxed security) |
| `ETCD_ALLOW_NONE_AUTH` | no | Set `yes` to disable etcd authentication (dev only) |
| `ENABLE_DEV_TOKEN_ENDPOINT` | false | Set `true` only for local dev |
| `SSL_VERIFY` | true | Set `false` if using self-signed certs |
| `RATE_LIMIT_PER_MINUTE` | 60 | Per-user query rate limit |
| `MAX_CONCURRENT_QUERIES` | 5 | Per-user concurrent query limit |

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

### 5. Access

| Service | URL |
|---------|-----|
| Web UI | `https://<HOST_IP>:<UNIBRIDGE_UI_PORT>` |
| Keycloak Admin | `https://<HOST_IP>:<KEYCLOAK_PORT>/admin` |
| API Gateway | `https://<HOST_IP>:<UNIBRIDGE_UI_PORT>/api/*` |
| LiteLLM | `https://<HOST_IP>:<LITELLM_PORT>` |
| Prometheus | `http://<HOST_IP>:9090` (localhost only) |

Default login: Keycloak admin console (`KC_ADMIN_USER` / `KC_ADMIN_PASSWORD`). No human users are seeded into the `apihub` realm by default. After first boot, sign in to the admin console and create the users you want in the `apihub` realm, then assign the roles and/or groups required for your deployment.

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
npm install
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

Run once after deploying the timezone-consistency changes to normalize legacy timestamp rows (pre-fix rows were stored at second precision without an offset; post-fix rows use microsecond precision and SQLite compares TEXT columns lexicographically, so the older shorter strings are excluded from boundary `>=` filters):

```bash
docker compose exec unibridge-service python -m scripts.backfill_utc_timestamps
```

The script introspects every `UtcDateTime` column and rewrites legacy values to the canonical microsecond form. It is idempotent — re-running it finds 0 rows to update.

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
