# Gateway Management Phase 2 — Consumer Management Design Spec

## Context

Phase 1에서 Routes + Upstreams + 서비스 키 관리를 구현했다. Phase 2에서는 API를 호출하는 클라이언트(Consumer)를 관리하고, 라우트에 인증을 연결한다. APISIX의 key-auth 플러그인을 사용하며, Consumer는 `apikey` 헤더로 인증한다.

## 인증 흐름

```
클라이언트 앱 ──apikey: abc123──▶ APISIX ──(key-auth 검증)──▶ upstream
```

1. Admin이 Consumer를 생성하고 API key를 발급
2. Admin이 라우트에 "Require Authentication" 활성화
3. 클라이언트가 `apikey: {발급받은 키}` 헤더로 요청
4. APISIX가 key-auth 플러그인으로 검증 → 성공 시 upstream 전달, 실패 시 401

## Architecture

Phase 1과 동일 — query-service가 APISIX Admin API를 프록시.

```
브라우저(JWT) → nginx → query-service(/admin/gateway/consumers/*) → APISIX Admin API
```

## APISIX Config 변경

`apisix/config.yaml`의 plugins 목록에 `key-auth` 추가:

```yaml
plugins:
  - jwt-auth
  - key-auth        # 추가
  - proxy-rewrite
  - prometheus
  ...
```

## Backend — Consumer Endpoints

### Consumer CRUD

| Method | Path | APISIX API | 설명 |
|--------|------|-----------|------|
| GET | `/admin/gateway/consumers` | `GET /apisix/admin/consumers` | Consumer 목록 |
| GET | `/admin/gateway/consumers/:username` | `GET /apisix/admin/consumers/:username` | Consumer 상세 |
| PUT | `/admin/gateway/consumers/:username` | `PUT /apisix/admin/consumers/:username` | Consumer 생성/수정 |
| DELETE | `/admin/gateway/consumers/:username` | `DELETE /apisix/admin/consumers/:username` | Consumer 삭제 |

### Consumer 생성/수정 시 body 변환

프론트엔드가 보내는 형식:

```json
{
  "username": "my-app",
  "api_key": "user-provided-or-auto-generated"
}
```

백엔드가 APISIX로 보내는 형식:

```json
{
  "username": "my-app",
  "plugins": {
    "key-auth": {
      "key": "user-provided-or-auto-generated"
    }
  }
}
```

### API Key 마스킹

- 목록/상세 조회 시: key를 마스킹하여 반환 (`***` + 마지막 4자)
- 생성 직후 응답: 전체 key를 한 번만 반환 (프론트엔드에서 복사 가능하게 표시)
- 수정 시: 빈 api_key를 보내면 기존 key 유지

### Route 인증 토글

GatewayRouteForm에서 "Require Authentication" 토글을 켜면, 백엔드의 `_inject_service_key` 로직을 확장하여 `key-auth: {}` 플러그인도 plugins에 merge한다.

프론트엔드가 보내는 추가 필드:

```json
{
  "require_auth": true
}
```

백엔드가 `require_auth`를 꺼내서:
- `true` → `plugins["key-auth"] = {}`
- `false` → `plugins`에서 `key-auth` 제거
- 필드 없음 → 기존 상태 유지

이 로직은 기존 `_inject_service_key` 함수를 `_inject_plugins`로 확장하여 처리한다.

## Frontend — GatewayConsumers Page

### 사이드바

Gateway 섹션에 추가:

```
Gateway Routes
Gateway Upstreams
Gateway Consumers    ← 추가
```

### 페이지 구조

Upstreams와 동일 패턴 — **목록 테이블 + 모달 CRUD**.

### 테이블 컬럼

| 컬럼 | 설명 |
|------|------|
| Username | Consumer 식별자 |
| API Key | 마스킹된 키 (`***xxxx`) |
| Actions | Edit, Delete 버튼 |

### 모달 폼

- **Username** (text input) — 생성 시 입력, 수정 시 disabled
- **API Key** (text input + "Generate" 버튼) — 자동 UUID 생성 또는 직접 입력. 수정 시 placeholder "Leave empty to keep current"

### 생성 직후 키 표시

Consumer 생성 성공 시, 모달을 닫지 않고 "API key가 생성되었습니다. 이 키는 다시 볼 수 없습니다." 메시지와 함께 전체 키를 표시. 복사 버튼 제공. 사용자가 확인 후 모달 닫기.

### GatewayRouteForm 수정

Service Key 섹션 위에 **Authentication 섹션** 추가:

```
┌─ Authentication ─────────────┐
│  [x] Require Authentication  │
│  (key-auth 플러그인 활성화)    │
└──────────────────────────────┘
```

체크박스 하나. 기존 라우트에 key-auth 플러그인이 있으면 체크된 상태로 로드.

## File Structure

### Backend

```
query-service/app/
  routers/gateway.py           # MODIFY: Consumer 엔드포인트 추가, _inject_plugins 확장
apisix/config.yaml             # MODIFY: key-auth 플러그인 추가
```

### Frontend

```
query-ui/src/
  api/client.ts                # MODIFY: Consumer 타입 + API 함수 추가
  components/Layout.tsx        # MODIFY: Gateway Consumers 메뉴 추가
  pages/
    GatewayConsumers.tsx       # CREATE: Consumer 목록 + 모달
    GatewayConsumers.css       # CREATE: Consumer 스타일
    GatewayRouteForm.tsx       # MODIFY: Authentication 토글 추가
  App.tsx                      # MODIFY: /gateway/consumers 라우트 추가
```

## What Does NOT Change

- apisix_client.py (기존 list/get/put/delete_resource 함수 재사용)
- 기존 5개 페이지 (Dashboard, Connections, Permissions, AuditLogs, QueryPlayground)
- GatewayRoutes 목록 페이지
- GatewayUpstreams 페이지
- 서비스 키 로직 (proxy-rewrite)
