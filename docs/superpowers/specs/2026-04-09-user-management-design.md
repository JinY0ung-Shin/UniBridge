# User Management — Design Spec

## Context

현재 사용자 생성/삭제/role 할당은 Keycloak Admin Console(`/admin`)에서만 가능하다. API Hub UI에 사용자 관리 페이지를 추가하여 Keycloak에 직접 접근하지 않아도 관리할 수 있도록 개선한다.

GitHub Issue: #1

## Scope

- 사용자 목록 조회 (검색/필터)
- 사용자 생성 (username, email, password, role 할당)
- 사용자 role 변경
- 사용자 비활성화/삭제
- 비밀번호 초기화

## Architecture

```
Frontend (Users.tsx)
    ↓ HTTP (Bearer token)
query-service (routers/users.py)
    ↓ Keycloak Admin REST API (Service Account token)
Keycloak
```

query-service가 프록시 역할을 한다. 사용자의 앱 권한(`admin.roles.write`)을 확인한 후, Service Account 토큰으로 Keycloak Admin API를 호출한다.

## Keycloak Service Account 설정

### Client 정의 (realm import JSON에 추가)

```json
{
  "clientId": "apihub-service",
  "enabled": true,
  "protocol": "openid-connect",
  "publicClient": false,
  "serviceAccountsEnabled": true,
  "directAccessGrantsEnabled": false,
  "standardFlowEnabled": false
}
```

### Service Account Role 할당

`apihub-service` service account에 `realm-management` client의 다음 role 부여:
- `manage-users` — 사용자 CRUD
- `view-users` — 사용자 목록 조회

이 설정은 Keycloak realm import JSON(`keycloak/realm-export.json`)에 포함하여 배포 시 자동 생성되도록 한다.

## Backend API

### Keycloak Admin Client 모듈

`app/keycloak_admin.py` — Service Account 토큰 관리 및 Keycloak Admin API 호출을 담당하는 모듈.

```python
class KeycloakAdminClient:
    """Keycloak Admin REST API client using Service Account credentials."""

    async def get_token(self) -> str
        """Client Credentials Grant로 access token 발급. 만료 전 캐싱."""

    async def list_users(self, search: str | None, first: int, max: int) -> list[dict]
    async def create_user(self, username: str, email: str, password: str, enabled: bool) -> str
    async def delete_user(self, user_id: str) -> None
    async def update_user(self, user_id: str, payload: dict) -> None
    async def reset_password(self, user_id: str, password: str, temporary: bool) -> None
    async def get_user_roles(self, user_id: str) -> list[dict]
    async def assign_realm_role(self, user_id: str, role_name: str) -> None
    async def remove_realm_role(self, user_id: str, role_name: str) -> None
```

토큰은 만료 시간 기반으로 캐싱하여 매 요청마다 재발급하지 않는다.

### Endpoints (`routers/users.py`)

모든 엔드포인트는 `admin.roles.write` 권한 필요.

#### `GET /admin/users`

사용자 목록 조회.

- Query params: `search` (optional), `first` (default 0), `max` (default 50)
- Keycloak API: `GET /admin/realms/{realm}/users?search=&first=&max=`
- 각 사용자의 realm role도 함께 조회하여 응답에 포함
- Response:

```json
{
  "users": [
    {
      "id": "keycloak-uuid",
      "username": "developer1",
      "email": "dev@example.com",
      "enabled": true,
      "role": "developer",
      "createdTimestamp": 1700000000000
    }
  ],
  "total": 25
}
```

#### `POST /admin/users`

사용자 생성 + role 할당.

- Request body:

```json
{
  "username": "newuser",
  "email": "new@example.com",
  "password": "tempPass123",
  "role": "developer"
}
```

- 처리 순서:
  1. Keycloak에 사용자 생성 (enabled: true)
  2. 임시 비밀번호 설정 (temporary: true → 첫 로그인 시 변경 필요)
  3. 지정된 realm role 할당
- Response: 생성된 사용자 정보 (201)

#### `PUT /admin/users/{user_id}/role`

사용자 role 변경.

- Request body:

```json
{
  "role": "admin"
}
```

- 처리 순서:
  1. 현재 할당된 realm role 조회
  2. 기존 앱 role(admin/developer/viewer 등) 제거
  3. 새 role 할당
- Response: 업데이트된 사용자 정보 (200)

#### `PUT /admin/users/{user_id}/reset-password`

비밀번호 초기화.

- Request body:

```json
{
  "password": "newTempPass",
  "temporary": true
}
```

- `temporary: true`면 다음 로그인 시 비밀번호 변경 강제
- Response: 204 No Content

#### `DELETE /admin/users/{user_id}`

사용자 삭제.

- 본인 계정 삭제 방지 (JWT의 sub claim과 비교)
- Response: 204 No Content

### Pydantic Schemas (`schemas.py`에 추가)

```python
class UserInfo(BaseModel):
    id: str
    username: str
    email: str | None
    enabled: bool
    role: str | None
    createdTimestamp: int | None

class UserListResponse(BaseModel):
    users: list[UserInfo]
    total: int

class CreateUserRequest(BaseModel):
    username: str  # min_length=1, max_length=100
    email: EmailStr | None = None
    password: str  # min_length=8
    role: str

class ChangeRoleRequest(BaseModel):
    role: str

class ResetPasswordRequest(BaseModel):
    password: str  # min_length=8
    temporary: bool = True
```

## Frontend

### 페이지 구조

`pages/Users.tsx` — Roles.tsx와 동일한 테이블 + 모달 패턴.

#### 목록 화면

- 페이지 헤더: "Users" 제목 + "+ Add User" 버튼
- 검색 입력창 (debounced)
- 테이블 컬럼: Username, Email, Role (badge), Status (Active/Disabled), Actions
- Actions: Edit (role 변경), Reset Password, Delete (본인 제외)
- Disabled 사용자는 행 스타일 구분

#### 생성 모달

- 필드: Username (text), Email (text, optional), Password (password), Role (드롭다운 단일 선택)
- Role 드롭다운은 기존 `getRoles()` API에서 role 목록을 가져와 표시
- Submit → `POST /admin/users`

#### Role 변경 모달

- 현재 role 표시 + 새 role 드롭다운 선택
- Submit → `PUT /admin/users/{id}/role`

#### 비밀번호 초기화 모달

- 새 비밀번호 입력 + "다음 로그인 시 변경 필요" 체크박스 (기본 ON)
- Submit → `PUT /admin/users/{id}/reset-password`

#### 삭제

- confirm 다이얼로그 후 `DELETE /admin/users/{id}`
- 현재 로그인한 사용자 본인은 삭제 버튼 비노출

### API Client 함수 (`api/client.ts`에 추가)

```typescript
// Users
export async function getUsers(params?: { search?: string; first?: number; max?: number }): Promise<UserListResponse>
export async function createUser(body: CreateUserRequest): Promise<UserInfo>
export async function changeUserRole(userId: string, role: string): Promise<UserInfo>
export async function resetUserPassword(userId: string, body: ResetPasswordRequest): Promise<void>
export async function deleteUser(userId: string): Promise<void>
```

### 라우팅 및 네비게이션

- Route: `/users` → `Users.tsx` (ProtectedRoute, permission: `admin.roles.read`)
- 사이드바 Admin 섹션에 "Users" 메뉴 추가 (Roles 옆)

### Permission

- 목록 조회: `admin.roles.read`
- 생성/수정/삭제: `admin.roles.write`

기존 `admin.roles.*` 권한을 재사용하여 별도 permission 추가 없이 구현한다.

## Error Handling

- Keycloak API 오류 → query-service에서 적절한 HTTP 상태코드로 변환
  - 409 Conflict: username 중복
  - 404 Not Found: 존재하지 않는 user_id
  - 403 Forbidden: Service Account 권한 부족
- Service Account 토큰 만료 → 자동 재발급 후 재시도
- 프론트엔드: axios error에서 detail 메시지 추출하여 표시 (기존 패턴 동일)

## Testing

- Backend: pytest로 Keycloak Admin Client 모킹 + 엔드포인트 테스트
- Frontend: 기존 테스트 패턴 따름
