# RBAC System — Phase 4 Design Spec

## Context

현재 인증 시스템은 하드코딩된 3개 role(admin, backend-dev, readonly)과 `require_admin` 단일 체크로 동작한다. Phase 4에서는 role을 자유롭게 생성/관리할 수 있는 RBAC(Role-Based Access Control) 시스템으로 전환한다.

## Permission 체계

```
query.databases.read       — DB 연결 목록 조회
query.databases.write      — DB 연결 추가/수정/삭제
query.permissions.read     — DB 권한 조회
query.permissions.write    — DB 권한 수정
query.audit.read           — Audit 로그 조회
query.execute              — Query Playground 실행
gateway.routes.read        — 라우트 조회
gateway.routes.write       — 라우트 추가/수정/삭제
gateway.upstreams.read     — 업스트림 조회
gateway.upstreams.write    — 업스트림 추가/수정/삭제
gateway.consumers.read     — Consumer 조회
gateway.consumers.write    — Consumer 추가/수정/삭제
gateway.monitoring.read    — 모니터링 대시보드
admin.roles.read           — Role 목록 조회
admin.roles.write          — Role 생성/수정/삭제
```

총 15개 permission. 모두 문자열 상수로 관리.

## DB Schema

### roles 테이블

| Column | Type | 설명 |
|--------|------|------|
| id | INTEGER PK AUTOINCREMENT | |
| name | VARCHAR(100) UNIQUE NOT NULL | role 이름 |
| description | VARCHAR(255) | 설명 |
| is_system | BOOLEAN DEFAULT FALSE | 시스템 기본 role (삭제 불가) |

### role_permissions 테이블

| Column | Type | 설명 |
|--------|------|------|
| id | INTEGER PK AUTOINCREMENT | |
| role_id | INTEGER FK → roles.id | |
| permission | VARCHAR(100) NOT NULL | permission 문자열 |

UNIQUE constraint: (role_id, permission)

### 시드 데이터

| Role | is_system | Permissions |
|------|-----------|-------------|
| admin | true | 전체 15개 |
| developer | true | query.databases.read, query.permissions.read, query.audit.read, query.execute, gateway.routes.read, gateway.upstreams.read, gateway.consumers.read, gateway.monitoring.read |
| viewer | true | gateway.monitoring.read, query.audit.read |

시스템 role은 삭제 불가, 권한 수정은 가능.

## Backend

### auth.py 변경

기존:
```python
async def require_admin(user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
    if user.role != "admin":
        raise HTTPException(403, "Admin role required")
    return user
```

변경 후:
```python
def require_permission(*perms: str):
    """FastAPI dependency factory: require any of the given permissions."""
    async def checker(
        user: CurrentUser = Depends(get_current_user),
        db: AsyncSession = Depends(get_db),
    ) -> CurrentUser:
        user_perms = await get_role_permissions(db, user.role)
        if not any(p in user_perms for p in perms):
            raise HTTPException(403, f"Required permission: {' or '.join(perms)}")
        return user
    return checker
```

`require_admin`은 `require_permission("admin.roles.write")`와 동일하지만 하위 호환을 위해 유지 — 내부적으로 admin role의 모든 permission을 체크.

### 새 라우터: routers/roles.py

| Method | Path | Permission | 설명 |
|--------|------|-----------|------|
| GET | `/admin/roles` | admin.roles.read | Role 목록 (permissions 포함) |
| GET | `/admin/roles/{id}` | admin.roles.read | Role 상세 |
| POST | `/admin/roles` | admin.roles.write | Role 생성 |
| PUT | `/admin/roles/{id}` | admin.roles.write | Role 수정 (이름, 설명, permissions) |
| DELETE | `/admin/roles/{id}` | admin.roles.write | Role 삭제 (is_system=true면 거부) |
| GET | `/admin/permissions` | admin.roles.read | 사용 가능한 전체 permission 목록 |
| GET | `/auth/roles` | (인증 불필요) | 로그인용 role 이름 목록 |

### 기존 엔드포인트 권한 매핑

| 엔드포인트 | 현재 | 변경 후 |
|-----------|------|---------|
| GET /admin/query/databases | require_admin | require_permission("query.databases.read") |
| POST /admin/query/databases | require_admin | require_permission("query.databases.write") |
| PUT /admin/query/databases/{alias} | require_admin | require_permission("query.databases.write") |
| DELETE /admin/query/databases/{alias} | require_admin | require_permission("query.databases.write") |
| GET /admin/query/permissions | require_admin | require_permission("query.permissions.read") |
| PUT /admin/query/permissions | require_admin | require_permission("query.permissions.write") |
| DELETE /admin/query/permissions/{id} | require_admin | require_permission("query.permissions.write") |
| GET /admin/query/audit-logs | require_admin | require_permission("query.audit.read") |
| POST /query/execute | get_current_user | require_permission("query.execute") |
| GET /admin/gateway/routes | require_admin | require_permission("gateway.routes.read") |
| PUT /admin/gateway/routes/{id} | require_admin | require_permission("gateway.routes.write") |
| DELETE /admin/gateway/routes/{id} | require_admin | require_permission("gateway.routes.write") |
| GET /admin/gateway/upstreams | require_admin | require_permission("gateway.upstreams.read") |
| PUT /admin/gateway/upstreams/{id} | require_admin | require_permission("gateway.upstreams.write") |
| DELETE /admin/gateway/upstreams/{id} | require_admin | require_permission("gateway.upstreams.write") |
| GET /admin/gateway/consumers | require_admin | require_permission("gateway.consumers.read") |
| PUT /admin/gateway/consumers/{id} | require_admin | require_permission("gateway.consumers.write") |
| DELETE /admin/gateway/consumers/{id} | require_admin | require_permission("gateway.consumers.write") |
| GET /admin/gateway/metrics/* | require_admin | require_permission("gateway.monitoring.read") |

### Permission 캐싱

role → permissions 매핑을 매 요청마다 DB 조회하면 비효율적. 간단한 인메모리 캐시 사용:
- dict[str, set[str]] — role_name → permission set
- TTL 60초 (60초마다 갱신)
- role 수정 시 캐시 무효화

### 유저 permissions 반환 API

프론트엔드가 현재 유저의 권한을 알아야 UI를 제어할 수 있으므로:

```
GET /auth/me → { username, role, permissions: ["query.execute", ...] }
```

JWT 인증 필요. 현재 유저의 role에 매핑된 permission 목록을 반환.

## Frontend

### API Client 확장

```typescript
interface RoleInfo {
  id: number;
  name: string;
  description: string;
  is_system: boolean;
  permissions: string[];
}

interface UserInfo {
  username: string;
  role: string;
  permissions: string[];
}

getRoles(): Promise<RoleInfo[]>
getRole(id: number): Promise<RoleInfo>
createRole(body): Promise<RoleInfo>
updateRole(id: number, body): Promise<RoleInfo>
deleteRole(id: number): Promise<void>
getAllPermissions(): Promise<string[]>
getAuthRoles(): Promise<string[]>  // 로그인용
getCurrentUser(): Promise<UserInfo>
```

### 로그인 폼 변경

하드코딩된 `<option>` → `/auth/roles`에서 role 목록을 불러와 동적으로 렌더링.

### Roles 관리 페이지

사이드바 최상단에 "Roles" 메뉴 추가 (admin.roles.read 권한 필요).

테이블: Name, Description, Permissions (개수 badge), System (badge), Actions
모달: Name, Description, Permission 체크박스 그리드 (카테고리별 그룹핑)

Permission 그리드 레이아웃:
```
┌─ Query ──────────────────────────────────┐
│  [x] databases.read  [x] databases.write │
│  [x] permissions.read [x] permissions.write │
│  [x] audit.read      [x] execute         │
├─ Gateway ────────────────────────────────┤
│  [x] routes.read     [x] routes.write    │
│  [x] upstreams.read  [x] upstreams.write │
│  [x] consumers.read  [x] consumers.write │
│  [x] monitoring.read                     │
├─ Admin ──────────────────────────────────┤
│  [x] roles.read      [x] roles.write     │
└──────────────────────────────────────────┘
```

### 사이드바 권한 기반 필터링

Layout에서 `/auth/me`를 호출하여 현재 유저의 permissions를 가져오고, 각 nav item에 필요한 permission을 매핑:

```typescript
const navItems = [
  { to: '/', label: 'Dashboard', section: 'data', permission: null }, // 모두 접근
  { to: '/connections', label: 'Connections', section: 'data', permission: 'query.databases.read' },
  { to: '/permissions', label: 'Permissions', section: 'data', permission: 'query.permissions.read' },
  // ...
];
```

permission이 없으면 해당 메뉴를 숨긴다.

### 페이지 내 write 권한 체크

- write 권한 없으면: 추가/수정/삭제 버튼 숨김
- 라우트 직접 접근 시도하면: 페이지는 보여주되 read-only 상태

## File Structure

### Backend

```
query-service/app/
  models.py              # MODIFY: Role, RolePermission 모델 추가
  schemas.py             # MODIFY: Role 스키마 추가
  auth.py                # MODIFY: require_permission, get_role_permissions, cache
  routers/roles.py       # CREATE: Role CRUD + /auth/me + /auth/roles
  routers/admin.py       # MODIFY: require_admin → require_permission
  routers/gateway.py     # MODIFY: require_admin → require_permission
  routers/query.py       # MODIFY: permission 체크 추가
  main.py                # MODIFY: roles router 등록
  database.py            # MODIFY: 시드 데이터 (init_db에서 기본 role 생성)
```

### Frontend

```
query-ui/src/
  api/client.ts          # MODIFY: role/permission 타입 + API 함수
  components/Layout.tsx   # MODIFY: 로그인 동적 role + 사이드바 permission 필터
  pages/Roles.tsx         # CREATE: Role 관리 페이지
  pages/Roles.css         # CREATE: Role 페이지 스타일
  App.tsx                 # MODIFY: /roles 라우트 추가
```

## What Does NOT Change

- JWT 토큰 구조 (여전히 {sub, role, exp})
- APISIX 관련 코드 (gateway router의 비즈니스 로직)
- 기존 페이지의 비즈니스 로직
- Prometheus / 모니터링 로직
