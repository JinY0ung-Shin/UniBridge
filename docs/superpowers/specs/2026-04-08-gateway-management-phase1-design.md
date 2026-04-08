# Gateway Management Phase 1 — Design Spec

## Context

API Hub Admin UI에 APISIX Gateway 관리 기능을 추가한다. Phase 1은 Routes + Upstreams CRUD와 라우트별 서비스 키 설정을 다룬다. 기존 APISIX 내장 Dashboard(9180/ui/)는 그대로 두고, 자주 쓰는 핵심 기능을 우리 UI에 통합한다.

### Phase 계획

- **Phase 1 (이번)**: Routes + Upstreams CRUD + 서비스 키
- Phase 2: Consumer 관리 (API 호출자 인증)
- Phase 3: 모니터링 (트래픽/에러율 대시보드)
- Phase 4: Admin UI 접근권한 확장

## Architecture

```
브라우저(JWT) → nginx(/api/) → query-service(/admin/gateway/*) → APISIX Admin API(9180)
                                       ↑ X-API-KEY 주입
```

- 프론트엔드는 APISIX Admin API를 직접 호출하지 않는다
- query-service가 프록시 역할 — JWT 인증 후 APISIX Admin API로 전달하며 X-API-KEY를 서버 측에서 주입
- API key가 브라우저에 노출되지 않음 (보안)
- 기존 role 기반 접근제어가 그대로 적용

### 환경변수

```
APISIX_ADMIN_URL=http://apisix:9180
APISIX_ADMIN_KEY=edd1c9f034335f136f87ad84b625c8f1
```

docker-compose.yml의 query-service environment에 추가.

## Backend — Gateway Proxy Endpoints

query-service(Python)에 `/admin/gateway/` 하위 엔드포인트를 추가한다. APISIX Admin API를 프록시하되 응답을 프론트엔드에 적합한 형태로 가공한다.

### Routes

| Method | Path | APISIX API | 설명 |
|--------|------|-----------|------|
| GET | `/admin/gateway/routes` | `GET /apisix/admin/routes` | 라우트 목록 |
| GET | `/admin/gateway/routes/:id` | `GET /apisix/admin/routes/:id` | 라우트 상세 |
| PUT | `/admin/gateway/routes/:id` | `PUT /apisix/admin/routes/:id` | 라우트 생성/수정 |
| DELETE | `/admin/gateway/routes/:id` | `DELETE /apisix/admin/routes/:id` | 라우트 삭제 |

### Upstreams

| Method | Path | APISIX API | 설명 |
|--------|------|-----------|------|
| GET | `/admin/gateway/upstreams` | `GET /apisix/admin/upstreams` | 업스트림 목록 |
| GET | `/admin/gateway/upstreams/:id` | `GET /apisix/admin/upstreams/:id` | 업스트림 상세 |
| PUT | `/admin/gateway/upstreams/:id` | `PUT /apisix/admin/upstreams/:id` | 업스트림 생성/수정 |
| DELETE | `/admin/gateway/upstreams/:id` | `DELETE /apisix/admin/upstreams/:id` | 업스트림 삭제 |

### ID 전략

라우트와 업스트림 모두 **클라이언트 지정 ID** 방식을 사용한다. `PUT /apisix/admin/routes/{id}`로 생성하며, 프론트엔드에서 ID를 생성한다 (타임스탬프 기반, 예: `Date.now().toString()`). APISIX의 POST(자동 생성) 방식은 사용하지 않는다.

### 라우트 수정 시 플러그인 보존

라우트 수정(PUT)은 전체 교체 방식이므로, 백엔드에서 반드시 다음 절차를 따른다:

1. 기존 라우트를 GET으로 조회
2. 기존 `plugins` 설정을 보존
3. 프론트엔드가 보낸 `service_key`를 `proxy-rewrite` 플러그인으로 변환하여 merge
4. 병합된 전체 라우트 데이터를 PUT으로 전송

이렇게 하면 Phase 2 이후 추가되는 플러그인(jwt-auth 등)이 수정 시 유실되지 않는다.

### Upstream 삭제 의존성

APISIX는 라우트가 참조 중인 upstream의 삭제를 거부한다. 백엔드에서 삭제 요청 시:

1. APISIX의 에러 응답(`400` 또는 `409`)을 감지
2. 프론트엔드에 "이 upstream을 참조하는 라우트가 있어 삭제할 수 없습니다" 메시지를 반환

강제 삭제(`force=true`)는 지원하지 않는다.

### Upstream 참조 방식

라우트는 반드시 `upstream_id`로 기존 upstream을 **참조**한다. 라우트 body에 인라인 upstream 설정(`upstream.nodes`)을 직접 넣는 것은 허용하지 않는다. 이는 Upstreams 페이지와의 일관성을 보장하고, 한 upstream을 여러 라우트가 공유할 수 있게 한다.

### 서비스 키 처리

라우트 생성/수정 시 프론트엔드가 보내는 형식:

```json
{
  "name": "OpenAI Proxy",
  "uri": "/api/openai/*",
  "methods": ["GET", "POST"],
  "upstream_id": "1",
  "status": 1,
  "service_key": {
    "header_name": "Authorization",
    "header_value": "Bearer sk-proj-abc123..."
  }
}
```

백엔드가 APISIX로 보내는 형식 (proxy-rewrite 플러그인으로 변환):

```json
{
  "name": "OpenAI Proxy",
  "uri": "/api/openai/*",
  "methods": ["GET", "POST"],
  "upstream_id": "1",
  "status": 1,
  "plugins": {
    "proxy-rewrite": {
      "headers": {
        "set": {
          "Authorization": "Bearer sk-proj-abc123..."
        }
      }
    }
  }
}
```

라우트 조회 시 백엔드는 `plugins.proxy-rewrite.headers.set`에서 서비스 키를 추출하고, 값의 마지막 4자를 제외한 나머지를 `***`로 마스킹하여 반환한다.

### 응답 가공

APISIX Admin API의 응답 형식:
```json
{
  "list": [
    { "key": "/apisix/routes/1", "value": { "id": "1", "uri": "/test", ... } }
  ],
  "total": 1
}
```

프론트엔드에 반환하는 형식 (value를 평탄화):
```json
{
  "items": [
    { "id": "1", "uri": "/test", ... }
  ],
  "total": 1
}
```

## Frontend — Pages & Routes

### 새 라우트 추가

```
/gateway/routes            → GatewayRoutes (목록)
/gateway/routes/new        → GatewayRouteForm (생성)
/gateway/routes/:id/edit   → GatewayRouteForm (수정)
/gateway/upstreams         → GatewayUpstreams (목록 + 모달 CRUD)
```

### 사이드바

기존 nav 아래에 구분선 + Gateway 섹션 추가:

```
Dashboard
Connections
Permissions
Audit Logs
Query Playground
──────────────────
Gateway Routes
Gateway Upstreams
```

### GatewayRoutes 페이지 (목록)

테이블 컬럼:
- Name — 라우트 이름
- URI — 매칭 패턴
- Methods — GET, POST 등 badge로 표시
- Upstream — 연결된 upstream 이름 또는 인라인 노드
- Status — 활성(1)/비활성(0) badge
- Actions — Edit, Delete 버튼

헤더에 "+ Add Route" primary 버튼.

### GatewayRouteForm 페이지 (생성/수정)

섹션별 구분:

**Basic Info**
- Name (text input)
- URI (text input, placeholder: `/api/service/*`)
- Methods (checkbox group: GET, POST, PUT, DELETE, PATCH)
- Status (toggle: 활성/비활성)

**Upstream**
- 기존 upstream 선택 (select dropdown, upstream 목록에서)
- 또는 "인라인으로 직접 입력" 토글 → host:port + weight 입력 필드

**Service Key (Optional)**
- Header Name (text input, placeholder: `Authorization`)
- Header Value (password input, placeholder: `Bearer sk-xxx...`)
- 수정 시 마스킹된 값 표시, 빈칸으로 두면 기존 값 유지

하단에 Cancel + Save 버튼. Cancel은 목록으로 돌아감.

### GatewayUpstreams 페이지 (목록 + 모달)

테이블 컬럼:
- Name — 업스트림 이름
- Type — roundrobin / chash 등
- Nodes — 서버 목록 (`host:port (weight)` 형태)
- Actions — Edit, Delete 버튼

헤더에 "+ Add Upstream" primary 버튼.

모달 폼:
- Name (text input)
- Type (select: roundrobin, chash)
- Nodes — 동적 리스트: host, port, weight 입력 + 추가/삭제 버튼

### API Client 확장

`src/api/client.ts`에 gateway 관련 타입과 함수 추가:

```typescript
// Types
interface GatewayRoute {
  id: string;
  name?: string;
  uri: string;
  methods?: string[];
  upstream_id?: string;
  status: number;
  service_key?: {
    header_name: string;
    header_value: string;  // 조회 시 마스킹됨
  };
}

interface GatewayUpstream {
  id: string;
  name?: string;
  type: string;
  nodes: Record<string, number>;  // "host:port": weight
}

interface GatewayListResponse<T> {
  items: T[];
  total: number;
}

// API functions
getGatewayRoutes(): Promise<GatewayListResponse<GatewayRoute>>
getGatewayRoute(id: string): Promise<GatewayRoute>
saveGatewayRoute(id: string, route: Partial<GatewayRoute>): Promise<GatewayRoute>
deleteGatewayRoute(id: string): Promise<void>

getGatewayUpstreams(): Promise<GatewayListResponse<GatewayUpstream>>
getGatewayUpstream(id: string): Promise<GatewayUpstream>
saveGatewayUpstream(id: string, upstream: Partial<GatewayUpstream>): Promise<GatewayUpstream>
deleteGatewayUpstream(id: string): Promise<void>
```

## File Structure

### Backend (query-service)

```
query-service/
  routers/
    gateway.py          # /admin/gateway/* 엔드포인트
  services/
    gateway_client.py   # APISIX Admin API 호출 래퍼
```

### Frontend (query-ui)

```
query-ui/src/
  pages/
    GatewayRoutes.tsx       # 라우트 목록
    GatewayRoutes.css
    GatewayRouteForm.tsx    # 라우트 생성/수정 폼
    GatewayRouteForm.css
    GatewayUpstreams.tsx    # 업스트림 목록 + 모달
    GatewayUpstreams.css
  api/
    client.ts               # gateway 타입/함수 추가
  components/
    Layout.tsx              # 사이드바에 Gateway 섹션 추가
    Layout.css              # 구분선 스타일
  App.tsx                   # 새 라우트 등록
```

## What Does NOT Change

- 기존 5개 페이지 (Dashboard, Connections, Permissions, AuditLogs, QueryPlayground)
- 기존 API client 함수들
- 로그인/인증 흐름
- APISIX config.yaml
- APISIX 내장 Dashboard (9180/ui/)

## Design System

기존 Vercel Dark 테마를 그대로 적용:
- 디자인 토큰: `shared.css`의 CSS custom properties 사용
- 테이블, 버튼, 배지, 모달, 폼: `shared.css`의 공통 스타일 재사용
- Methods badge: `badge-type` 스타일 활용 (GET=blue, POST=green, PUT=yellow, DELETE=red 변형 가능)
- Status toggle: checkbox 또는 커스텀 토글 UI
