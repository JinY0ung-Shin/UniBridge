from datetime import datetime
from typing import Any

import ipaddress
from urllib.parse import urlparse

from pydantic import BaseModel, Field, field_validator


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
    db_type: str = Field(..., pattern=r"^(postgres|mssql|clickhouse)$")
    host: str = Field(..., min_length=1)
    port: int = Field(..., ge=1, le=65535)
    database: str = Field(..., min_length=1)
    username: str = Field(..., min_length=1)
    password: str = Field(..., min_length=1)
    protocol: str | None = Field(None, pattern=r"^(http|https)$")
    secure: bool | None = None
    pool_size: int | None = Field(5, ge=1, le=50)
    max_overflow: int | None = Field(3, ge=0, le=50)
    query_timeout: int | None = Field(30, ge=1, le=300)


class DBConnectionUpdate(BaseModel):
    host: str | None = None
    port: int | None = Field(None, ge=1, le=65535)
    database: str | None = None
    username: str | None = None
    password: str | None = None
    protocol: str | None = Field(None, pattern=r"^(http|https)$")
    secure: bool | None = None
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
    protocol: str | None = None
    secure: bool | None = None
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
    allowed_tables: list[str] | None = None


class PermissionResponse(BaseModel):
    id: int
    role: str
    db_alias: str
    allow_select: bool
    allow_insert: bool
    allow_update: bool
    allow_delete: bool
    allowed_tables: list[str] | None = None

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


# ── System Config ───────────────────────────────────────────────────────────

class SystemConfigResponse(BaseModel):
    rate_limit_per_minute: int
    max_concurrent_queries: int
    blocked_sql_keywords: list[str]


class SystemConfigUpdate(BaseModel):
    rate_limit_per_minute: int | None = Field(None, ge=1, le=1000)
    max_concurrent_queries: int | None = Field(None, ge=1, le=100)
    blocked_sql_keywords: list[str] | None = Field(None, description="Each keyword must be non-empty")

    @staticmethod
    def _validate_keywords(v: list[str] | None) -> list[str] | None:
        if v is not None:
            v = [kw.strip() for kw in v if kw.strip()]
        return v if v else None

    def model_post_init(self, __context: object) -> None:
        if self.blocked_sql_keywords is not None:
            object.__setattr__(
                self,
                "blocked_sql_keywords",
                self._validate_keywords(self.blocked_sql_keywords),
            )


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


# ── API Keys ────────────────────────────────────────────────────────────────

class ApiKeyCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100, description="Unique key name (becomes APISIX consumer username)")
    description: str = ""
    api_key: str | None = Field(None, description="Custom API key value; auto-generated if omitted")
    allowed_databases: list[str] = Field(default_factory=list, description="Database aliases this key can query")
    allowed_routes: list[str] = Field(default_factory=list, description="Gateway route IDs this key can access")


class ApiKeyUpdate(BaseModel):
    description: str | None = None
    api_key: str | None = Field(None, description="New API key; omit to keep current")
    allowed_databases: list[str] | None = None
    allowed_routes: list[str] | None = None


class ApiKeyResponse(BaseModel):
    name: str
    description: str
    api_key: str | None = None
    key_created: bool = False
    allowed_databases: list[str]
    allowed_routes: list[str]
    created_at: datetime | None = None

    model_config = {"from_attributes": True}


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


# ── Alerts ──────────────────────────────────────────────────────────────────

_BLOCKED_HOSTNAMES = frozenset({
    "localhost", "keycloak", "etcd", "apisix",
    "litellm", "prometheus", "unibridge-service",
    "keycloak-db", "litellm-db",
    "metadata.google.internal",
})


def _validate_webhook_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("webhook_url must use http or https scheme")
    hostname = parsed.hostname
    if not hostname:
        raise ValueError("webhook_url must include a hostname")
    # Block internal Docker service names and cloud metadata
    if hostname.lower() in _BLOCKED_HOSTNAMES:
        raise ValueError("webhook_url cannot target internal services")
    # Block private/loopback/link-local IPs
    try:
        ip = ipaddress.ip_address(hostname)
        if ip.is_private or ip.is_loopback or ip.is_link_local:
            raise ValueError("webhook_url cannot target private/internal addresses")
    except ValueError as exc:
        if "cannot target" in str(exc):
            raise
        # hostname is not an IP — already checked against blocklist above
    # Block 169.254.169.254 (cloud metadata)
    if hostname == "169.254.169.254":
        raise ValueError("webhook_url cannot target cloud metadata endpoint")
    return url


class AlertChannelCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    webhook_url: str = Field(..., min_length=1)
    payload_template: str = Field(..., min_length=1)
    headers: dict[str, str] | None = None
    enabled: bool = True

    @field_validator("webhook_url")
    @classmethod
    def check_webhook_url(cls, v: str) -> str:
        return _validate_webhook_url(v)


class AlertChannelUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=100)
    webhook_url: str | None = Field(None, min_length=1)
    payload_template: str | None = None
    headers: dict[str, str] | None = None
    enabled: bool | None = None

    @field_validator("webhook_url")
    @classmethod
    def check_webhook_url(cls, v: str | None) -> str | None:
        if v is not None:
            return _validate_webhook_url(v)
        return v


class AlertChannelResponse(BaseModel):
    id: int
    name: str
    webhook_url: str
    payload_template: str
    headers: dict[str, str] | None = None
    enabled: bool
    created_at: datetime | None = None
    updated_at: datetime | None = None

    model_config = {"from_attributes": True}


class RuleChannelMapping(BaseModel):
    channel_id: int
    recipients: list[str]


class AlertRuleCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    type: str = Field(..., pattern=r"^(db_health|upstream_health|error_rate)$")
    target: str = Field(..., min_length=1, max_length=100)
    threshold: float | None = Field(None, ge=0, le=100)
    enabled: bool = True
    channels: list[RuleChannelMapping] = Field(default_factory=list)


class AlertRuleUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=100)
    type: str | None = Field(None, pattern=r"^(db_health|upstream_health|error_rate)$")
    target: str | None = Field(None, min_length=1, max_length=100)
    threshold: float | None = None
    enabled: bool | None = None
    channels: list[RuleChannelMapping] | None = None


class RuleChannelDetail(BaseModel):
    channel_id: int
    channel_name: str
    recipients: list[str]


class AlertRuleResponse(BaseModel):
    id: int
    name: str
    type: str
    target: str
    threshold: float | None = None
    enabled: bool
    channels: list[RuleChannelDetail] = []
    created_at: datetime | None = None
    updated_at: datetime | None = None

    model_config = {"from_attributes": True}


class AlertHistoryResponse(BaseModel):
    id: int
    rule_id: int | None = None
    channel_id: int | None = None
    alert_type: str
    target: str
    message: str
    recipients: list[str] | None = None
    sent_at: datetime | None = None
    success: bool | None = None
    error_detail: str | None = None

    model_config = {"from_attributes": True}


class AlertStatusResponse(BaseModel):
    target: str
    type: str
    status: str  # "ok" | "alert"
    since: str | None = None


# ── S3 Connections ──────────────────────────────────────────────────────────

class S3ConnectionCreate(BaseModel):
    alias: str = Field(..., min_length=1, max_length=100)
    endpoint_url: str | None = Field(None, description="Custom endpoint for S3-compatible storage (MinIO, R2, etc.)")
    region: str = Field("us-east-1", min_length=1, max_length=100)
    access_key_id: str = Field(..., min_length=1)
    secret_access_key: str = Field(..., min_length=1)
    default_bucket: str | None = Field(None, max_length=255)
    use_ssl: bool = True

    @field_validator("endpoint_url")
    @classmethod
    def check_endpoint_url(cls, v: str | None) -> str | None:
        if v is not None and v.strip():
            return _validate_webhook_url(v)
        return None


class S3ConnectionUpdate(BaseModel):
    endpoint_url: str | None = None
    region: str | None = Field(None, min_length=1, max_length=100)
    access_key_id: str | None = Field(None, min_length=1)
    secret_access_key: str | None = Field(None, min_length=1)
    default_bucket: str | None = Field(None, max_length=255)
    use_ssl: bool | None = None

    @field_validator("endpoint_url")
    @classmethod
    def check_endpoint_url(cls, v: str | None) -> str | None:
        if v is not None and v.strip():
            return _validate_webhook_url(v)
        return None


class S3ConnectionResponse(BaseModel):
    alias: str
    endpoint_url: str | None = None
    region: str
    access_key_id_masked: str = ""
    default_bucket: str | None = None
    use_ssl: bool
    status: str = "unknown"

    model_config = {"from_attributes": True}
