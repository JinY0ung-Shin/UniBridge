from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


# ── Query ────────────────────────────────────────────────────────────────────

class QueryRequest(BaseModel):
    database: str = Field(..., description="Database alias to run the query against")
    sql: str = Field(..., description="SQL statement to execute")
    params: dict[str, Any] | None = Field(None, description="Named bind parameters")
    limit: int | None = Field(None, ge=1, description="Maximum number of rows to return")
    timeout: int | None = Field(None, ge=1, description="Query timeout in seconds")


class QueryResponse(BaseModel):
    columns: list[str]
    rows: list[list[Any]]
    row_count: int
    truncated: bool
    elapsed_ms: int


# ── DB Connections ───────────────────────────────────────────────────────────

class DBConnectionCreate(BaseModel):
    alias: str = Field(..., min_length=1, max_length=100)
    db_type: str = Field(..., pattern=r"^(postgres|mssql)$")
    host: str = Field(..., min_length=1)
    port: int = Field(..., ge=1, le=65535)
    database: str = Field(..., min_length=1)
    username: str = Field(..., min_length=1)
    password: str = Field(..., min_length=1)
    pool_size: int | None = Field(5, ge=1, le=50)
    max_overflow: int | None = Field(3, ge=0, le=50)
    query_timeout: int | None = Field(30, ge=1, le=300)


class DBConnectionUpdate(BaseModel):
    host: str | None = None
    port: int | None = Field(None, ge=1, le=65535)
    database: str | None = None
    username: str | None = None
    password: str | None = None
    pool_size: int | None = Field(None, ge=1, le=50)
    max_overflow: int | None = Field(None, ge=0, le=50)
    query_timeout: int | None = Field(None, ge=1, le=300)


class DBConnectionResponse(BaseModel):
    alias: str
    db_type: str
    host: str
    port: int
    database: str
    username: str
    pool_size: int
    max_overflow: int
    query_timeout: int
    status: str = "unknown"

    model_config = {"from_attributes": True}


# ── Permissions ──────────────────────────────────────────────────────────────

class PermissionCreate(BaseModel):
    role: str = Field(..., min_length=1)
    db_alias: str = Field(..., min_length=1)
    allow_select: bool = True
    allow_insert: bool = False
    allow_update: bool = False
    allow_delete: bool = False


class PermissionResponse(BaseModel):
    id: int
    role: str
    db_alias: str
    allow_select: bool
    allow_insert: bool
    allow_update: bool
    allow_delete: bool

    model_config = {"from_attributes": True}


# ── Audit Logs ───────────────────────────────────────────────────────────────

class AuditLogResponse(BaseModel):
    id: int
    timestamp: datetime | None = None
    user: str
    database_alias: str
    sql: str
    params: str | None = None
    row_count: int | None = None
    elapsed_ms: int | None = None
    status: str
    error_message: str | None = None

    model_config = {"from_attributes": True}


class AuditLogQuery(BaseModel):
    database: str | None = None
    user: str | None = None
    from_date: datetime | None = None
    to_date: datetime | None = None
    limit: int = Field(100, ge=1, le=1000)
    offset: int = Field(0, ge=0)


# ── Health ───────────────────────────────────────────────────────────────────

class HealthResponse(BaseModel):
    status: str
    databases: dict[str, Any] = {}


# ── Roles (RBAC) ────────────────────────────────────────────────────────────

class RoleCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    description: str = ""
    permissions: list[str] = []


class RoleUpdate(BaseModel):
    description: str | None = None
    permissions: list[str] | None = None


class RoleResponse(BaseModel):
    id: int
    name: str
    description: str
    is_system: bool
    permissions: list[str]

    model_config = {"from_attributes": True}


class UserInfoResponse(BaseModel):
    username: str
    role: str
    permissions: list[str]


# ── Auth (dev/testing) ───────────────────────────────────────────────────────

class TokenRequest(BaseModel):
    username: str
    role: str = "viewer"


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


# ── Users (Keycloak) ────────────────────────────────────────────────────────

class KeycloakUser(BaseModel):
    id: str
    username: str
    email: str | None = None
    enabled: bool = True
    role: str | None = None
    createdTimestamp: int | None = None


class KeycloakUserList(BaseModel):
    users: list[KeycloakUser]
    total: int


class CreateUserRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=100)
    email: str | None = None
    password: str = Field(..., min_length=8)
    role: str = Field(..., min_length=1)


class ChangeRoleRequest(BaseModel):
    role: str = Field(..., min_length=1)


class ResetPasswordRequest(BaseModel):
    password: str = Field(..., min_length=8)
    temporary: bool = True


class ToggleEnabledRequest(BaseModel):
    enabled: bool
