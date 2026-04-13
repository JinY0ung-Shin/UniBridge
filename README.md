# API Hub (UniBridge)

Internal API/DB gateway platform. Register multiple databases (PostgreSQL, MSSQL), execute SQL through a single endpoint, manage API routes via APISIX, and control access with RBAC + API keys.

## Architecture

```
Browser ──HTTPS──> query-ui (nginx)
                       │
                       ├── /_api/* ──> query-service (FastAPI)
                       └── /api/*  ──> apisix (API Gateway)
                                          │
                                          ├── Registered databases
                                          └── Upstream services

Keycloak ── OIDC auth
Prometheus ── APISIX metrics
```

**Services (7):** etcd, APISIX, Keycloak + Postgres, query-service, Prometheus, query-ui

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

**Must change:**

| Variable | Description |
|----------|-------------|
| `HOST_IP` | Server IP or hostname that browsers access (not `localhost` in production) |
| `ENCRYPTION_KEY` | `python -c "import secrets; print(secrets.token_urlsafe(32))"` |
| `JWT_SECRET` | Same command as above, different value |
| `APISIX_ADMIN_KEY` | Random string for APISIX admin API |
| `ETCD_ROOT_PASSWORD` | etcd root password (APISIX config store authentication) |
| `KC_ADMIN_PASSWORD` | Keycloak admin console password |
| `KC_DB_PASSWORD` | Keycloak database password |
| `KEYCLOAK_SERVICE_CLIENT_SECRET` | `python -c "import secrets; print(secrets.token_urlsafe(32))"` |

**Optional:**

| Variable | Default | Description |
|----------|---------|-------------|
| `QUERY_UI_PORT` | 3000 | HTTPS port for the web UI |
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
| Web UI | `https://<HOST_IP>:<QUERY_UI_PORT>` |
| Keycloak Admin | `https://<HOST_IP>:<KEYCLOAK_PORT>/admin` |
| API Gateway | `https://<HOST_IP>:<QUERY_UI_PORT>/api/*` |
| Prometheus | `http://<HOST_IP>:9090` (localhost only) |

Default login: Keycloak admin console (`KC_ADMIN_USER` / `KC_ADMIN_PASSWORD`), then create a user in the `apihub` realm.

## Service Ports (default)

| Port | Service | Binding |
|------|---------|---------|
| 3000 | query-ui (HTTPS) | public |
| 8443 | Keycloak (HTTPS) | public |
| 8000 | query-service | localhost only |
| 9180 | APISIX admin | localhost only |
| 9090 | Prometheus | localhost only |

## Local Development (without Docker)

### Backend

```bash
cd query-service
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
pip install pytest pytest-asyncio pytest-cov

# Minimal .env for dev
export META_DB_URL="sqlite+aiosqlite:///data/meta.db"
export ENCRYPTION_KEY="dev-key-change-in-prod-32chars!"
export JWT_SECRET="dev-jwt-secret"
export ENABLE_DEV_TOKEN_ENDPOINT=true

uvicorn app.main:app --reload --port 8000
```

### Frontend

```bash
cd query-ui
npm install
npm run dev   # http://localhost:5173, proxies /_api to :8000
```

### Tests

```bash
# Backend
cd query-service && pytest tests/ -v

# Frontend
cd query-ui && npx vitest run
```

## Common Operations

```bash
# Restart all
docker compose restart

# Rebuild after code change
docker compose up -d --build

# View logs
docker compose logs -f query-service
docker compose logs -f keycloak

# Stop
docker compose down

# Stop and remove volumes (DESTROYS DATA)
docker compose down -v
```

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

- **Multi-DB support** — PostgreSQL, MSSQL via a single query endpoint
- **APISIX Gateway** — Route management, upstream config, API key auth
- **RBAC** — 17 granular permissions, dynamic role management
- **API Keys** — External access with per-database/route restrictions
- **Monitoring** — Prometheus metrics, request trends, latency percentiles
- **User Management** — Keycloak integration, role assignment
- **Audit Logging** — Full query history with filters
- **i18n** — Korean / English
