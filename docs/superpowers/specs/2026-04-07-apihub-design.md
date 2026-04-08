# API Hub - 통합 API/DB 게이트웨이 설계

## 개요

여러 DB(Postgres, MSSQL)와 REST API 서비스를 하나의 엔드포인트로 묶어 관리하는 내부 개발자용 플랫폼.

**규모**: DB 4~5개 (Postgres, MSSQL) + REST API ~10개
**사용자**: 내부 개발자
**핵심 요구사항**:
- 단일 진입점 (API Gateway)
- DB를 API로 노출 (임의 SQL 실행 포함)
- 모니터링, 설정, 접근 제어 관리

## 아키텍처

```
                    ┌─────────────────────────────┐
                    │      Internal Developers     │
                    └──────────────┬───────────────┘
                                   │
                          apihub.internal:9080
                                   │
                    ┌──────────────▼───────────────┐
                    │        Apache APISIX          │
                    │   (API Gateway / Router)      │
                    │  - 인증 (JWT / API Key)        │
                    │  - 레이트리밋                    │
                    │  - 로깅 / 메트릭               │
                    │  - 라우팅                      │
                    └──┬───────────────────────┬────┘
                       │                       │
          ┌────────────▼──────────┐  ┌─────────▼──────────┐
          │    Query Service      │  │  Upstream REST APIs │
          │    (FastAPI)          │  │  (기존 10개 서비스)   │
          │  - 임의 SQL 실행       │  │                     │
          │  - DB 커넥션 관리      │  │  service-a:8080     │
          │  - 권한 관리           │  │  service-b:8081     │
          │  - 감사 로그           │  │  ...                │
          └──┬──────────────┬─────┘  └─────────────────────┘
             │              │
      ┌──────▼───┐   ┌─────▼────┐
      │ Postgres │   │  MSSQL   │
      │ (N개)    │   │  (N개)   │
      └──────────┘   └──────────┘
```

## 컴포넌트 상세

### 컴포넌트 구성

| 컴포넌트 | 기술 스택 | 포트 | 역할 |
|----------|----------|------|------|
| APISIX | Nginx + etcd | 9080 (API), 9180 (Admin + Dashboard) | 단일 진입점, 라우팅, 인증, 플러그인, 내장 관리 UI |
| Query Service | FastAPI + SQLAlchemy | 8000 | 임의 SQL 실행, DB 커넥션 관리, 권한, 감사 로그 |
| Query Service UI | React (Vite) | 3000 | Query Service 관리 UI |
| etcd | etcd | 2379 | APISIX 설정 저장소 |

### 라우팅 체계

```
apihub.internal:9080
  │
  ├── /api/{service-name}/*   → 기존 REST API 프록시 (10개 서비스)
  │
  ├── /query/execute          → Query Service (SQL 실행)
  ├── /query/databases        → Query Service (연결된 DB 목록)
  ├── /query/health           → Query Service (헬스체크)
  │
  ├── /admin/gateway/*        → APISIX 내장 Dashboard (9180/ui/)
  └── /admin/query/*          → Query Service 관리 UI
```

## 인증 및 접근 제어

### 인증 방식

JWT 기반 단일 인증. APISIX에서 한 번 검증, downstream 서비스는 신뢰.

```
개발자 → POST /auth/login { id, password } → JWT 발급

이후 모든 요청:
  Authorization: Bearer <JWT>
  → APISIX jwt-auth 플러그인에서 검증
  → 통과하면 upstream으로 전달
```

### 접근 제어

| 레벨 | 적용 위치 | 방식 |
|------|----------|------|
| 라우트 접근 | APISIX | consumer-group 기반 |
| SQL 실행 권한 | Query Service | DB alias별 허용 목록 + DML 제어 |

### 역할 예시

```yaml
roles:
  admin:
    - /api/*          # 모든 REST API
    - /query/*        # 모든 DB 쿼리
    - /admin/*        # 관리 UI

  backend-dev:
    - /api/*          # 모든 REST API
    - /query/execute  # SQL 실행 (허용된 DB만)

  readonly:
    - /api/* (GET)    # REST API 조회만
    - /query/execute  # SELECT만 허용
```

### Backend 서비스 / DB 인증

개발자는 자기 JWT만 관리. 뒤의 서비스/DB 인증은 플랫폼이 대리 처리.

| 대상 | 인증 주체 | 개발자가 알아야 하는 것 |
|------|----------|----------------------|
| APISIX 진입 | 개발자 본인 JWT | 자기 JWT 토큰 |
| 기존 REST API | APISIX proxy-rewrite로 서비스 토큰 주입 | 없음 |
| DB | Query Service 내부 커넥션 풀 | DB alias 이름만 |

서비스별 토큰은 APISIX Secret Manager (Vault 등) 연동으로 안전하게 관리.

## Query Service 상세 설계

### 기술 스택

| 구성 | 선택 | 이유 |
|------|------|------|
| 프레임워크 | FastAPI | 비동기, 자동 OpenAPI 문서, 타입 검증 |
| DB 연결 | SQLAlchemy 2.0 + asyncpg/aioodbc | Postgres/MSSQL 모두 지원, 비동기 커넥션 풀 |
| 관리 UI | React (Vite) | 내부 개발자 친숙, 빠른 개발 |
| 메타데이터 저장 | SQLite 또는 Postgres | 커넥션 정보, 역할, 권한, 감사 로그 |

### API 스펙

```
# SQL 실행
POST /query/execute
{
  "database": "sales-pg",
  "sql": "SELECT * FROM orders WHERE created_at > :date",
  "params": { "date": "2026-01-01" },
  "limit": 1000,
  "timeout": 15
}

→ 200 OK
{
  "columns": ["id", "product", "amount", "created_at"],
  "rows": [[1, "Widget", 5000, "2026-03-01"], ...],
  "row_count": 847,
  "truncated": false,
  "elapsed_ms": 42
}

# DB 커넥션 목록
GET /query/databases
→ [
    { "alias": "sales-pg", "type": "postgres", "host": "10.0.1.5", "status": "connected" },
    { "alias": "hr-mssql", "type": "mssql", "host": "10.0.2.3", "status": "connected" }
  ]

# DB 커넥션 관리 (Admin)
POST   /admin/query/databases              # 추가
PUT    /admin/query/databases/{alias}       # 수정
DELETE /admin/query/databases/{alias}       # 삭제
POST   /admin/query/databases/{alias}/test  # 연결 테스트

# 역할별 DB 권한 관리 (Admin)
GET    /admin/query/permissions
PUT    /admin/query/permissions/{role}

# 감사 로그 조회 (Admin)
GET    /admin/query/audit-logs?database=sales-pg&user=kim&from=2026-04-01
```

### DB 커넥션 관리

```yaml
# 내부 저장 구조 (메타 DB)
connections:
  - alias: "sales-pg"
    type: postgres
    host: 10.0.1.5
    port: 5432
    database: sales
    username: apihub_ro
    password: <encrypted>
    pool_size: 10
    max_overflow: 5

  - alias: "hr-mssql"
    type: mssql
    host: 10.0.2.3
    port: 1433
    database: hr_db
    username: apihub_reader
    password: <encrypted>
    pool_size: 5
    max_overflow: 3
```

- 비밀번호 암호화 저장 (AES or Vault 연동)
- 커넥션 풀은 alias별 독립 관리
- 커넥션 추가/변경 시 연결 테스트 후 반영
- Hot reload — 서비스 재시작 없이 커넥션 추가/삭제 가능

### 보안

```
요청 흐름:
  JWT에서 role 추출 → DB alias 접근 권한 확인 → SQL 파싱 → 실행

보안 레이어:
  1) 권한 체크     : role=backend-dev → sales-pg(SELECT), hr-mssql(거부)
  2) SQL 파싱      : DML 허용 여부 확인 (SELECT만? INSERT/UPDATE도?)
  3) 파라미터 바인딩 : :name 방식 강제, string concat 차단
  4) 타임아웃      : DB별 최대 실행 시간 제한
  5) Row 제한      : 최대 반환 건수 제한 (기본 10,000건)
  6) 감사 로그     : 모든 실행 기록 (user, db, sql, elapsed, row_count)
```

### 관리 UI 화면 구성

```
Query Service Admin (/admin/query/)
  │
  ├── 대시보드        — DB 연결 상태, 최근 쿼리 통계
  ├── DB 커넥션 관리   — 추가/수정/삭제/연결 테스트
  ├── 권한 관리       — 역할별 DB 접근, DML 허용 설정
  └── 감사 로그       — 쿼리 실행 이력 검색/조회
```

## 배포 구성

### 디렉토리 구조

```
apihub/
  ├── docker-compose.yml
  ├── apisix/
  │     └── config.yaml
  ├── query-service/
  │     ├── Dockerfile
  │     ├── app/
  │     └── requirements.txt
  ├── query-ui/
  │     ├── Dockerfile
  │     └── src/
  └── .env
```

### Docker Compose

```yaml
services:
  # --- Gateway ---
  apisix:
    image: apache/apisix:3.15.0-debian
    ports:
      - "9080:9080"     # API 진입점
      - "9180:9180"     # Admin API + 내장 Dashboard (/ui/)
    depends_on: [etcd]

  etcd:
    image: bitnamilegacy/etcd:3.5.11

  # --- Query Service ---
  query-service:
    build: ./query-service
    ports:
      - "8000:8000"
    environment:
      - META_DB_URL=sqlite:///data/meta.db
      - ENCRYPTION_KEY=${ENCRYPTION_KEY}
      - JWT_SECRET=${JWT_SECRET}
    volumes:
      - query-data:/data

  query-ui:
    build: ./query-ui
    ports:
      - "3000:3000"

volumes:
  query-data:
```

### 네트워크 흐름

```
외부 접근:
  :9080  → APISIX (유일한 API 진입점)
  :9180  → APISIX Admin API + 내장 Dashboard (/ui/) (관리자만)
  :3000  → Query Service 관리 UI (관리자만)

내부 통신 (docker network):
  APISIX → query-service:8000
  APISIX → 기존 REST API 서비스들
  query-service → Postgres/MSSQL (각 DB 서버)
```

## 모니터링 및 로깅

### APISIX 플러그인

```yaml
plugins:
  prometheus:        # 메트릭 → Grafana 연동 가능
  http-logger:       # 요청/응답 로그
  error-log-logger:  # 에러 로그
```

### Query Service 로깅

감사 로그 (meta DB 저장):
- timestamp, user, database alias, sql, params
- row_count, elapsed_ms, status, error_message

애플리케이션 로그 (stdout → Docker logs):
- 커넥션 풀 상태, DB 연결 실패/복구, 타임아웃 발생

### 헬스체크

```
GET /health              → Query Service 자체 상태
GET /health/databases    → 각 DB 커넥션 상태

응답:
{
  "status": "healthy",
  "databases": {
    "sales-pg": { "status": "connected", "pool_active": 3, "pool_idle": 7 },
    "hr-mssql": { "status": "connected", "pool_active": 1, "pool_idle": 4 }
  }
}
```

APISIX upstream 헬스체크를 /health에 연결하여 Query Service 장애 자동 감지.

## 향후 확장 고려사항

- 통합 관리 UI (APISIX Dashboard + Query Service Admin을 하나로 병합)
- GraphQL 지원
- 쿼리 캐싱
- API 버전 관리
