from datetime import datetime
from typing import Any
from urllib.parse import urlparse

from pydantic import BaseModel, Field, field_validator, model_validator

from app.services.webhook_security import validate_webhook_url


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
    db_type: str = Field(..., pattern=r"^(postgres|mssql|clickhouse|neo4j)$")
    host: str = Field(..., min_length=1)
    port: int = Field(..., ge=1, le=65535)
    database: str = Field(..., min_length=1)
    username: str = Field(..., min_length=1)
    password: str = Field(..., min_length=1)
    protocol: str | None = Field(None, pattern=r"^(http|https|bolt|bolt\+s|bolt\+ssc|neo4j|neo4j\+s|neo4j\+ssc)$")
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
    protocol: str | None = Field(None, pattern=r"^(http|https|bolt|bolt\+s|bolt\+ssc|neo4j|neo4j\+s|neo4j\+ssc)$")
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

def _validate_webhook_url(url: str) -> str:
    return validate_webhook_url(url)


def _validate_s3_endpoint_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("endpoint_url must use http or https scheme")
    if not parsed.hostname:
        raise ValueError("endpoint_url must include a hostname")
    return url


class AlertChannelCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    webhook_url: str = Field(..., min_length=1)
    payload_template: str = Field(..., min_length=1)
    recipient_item_template: str | None = None
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
    recipient_item_template: str | None = None
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
    recipient_item_template: str | None = None
    headers: dict[str, str] | None = None
    enabled: bool
    created_at: datetime | None = None
    updated_at: datetime | None = None

    model_config = {"from_attributes": True}


class AlertSettingsResponse(BaseModel):
    mail_channel_id: int | None = None
    fallback_owner_group_id: int | None = None
    route_error_threshold_pct: float
    check_interval_seconds: int
    updated_at: datetime | None = None

    model_config = {"from_attributes": True}


class AlertSettingsUpdate(BaseModel):
    mail_channel_id: int | None = None
    fallback_owner_group_id: int | None = None
    route_error_threshold_pct: float | None = Field(None, ge=0, le=100)
    check_interval_seconds: int | None = Field(None, ge=30, le=3600)

    @model_validator(mode="after")
    def reject_explicit_numeric_nulls(self) -> "AlertSettingsUpdate":
        for field_name in ("route_error_threshold_pct", "check_interval_seconds"):
            if field_name in self.model_fields_set and getattr(self, field_name) is None:
                raise ValueError(f"{field_name} cannot be null")
        return self


class FallbackOwnerGroupTestRequest(BaseModel):
    mail_channel_id: int
    fallback_owner_group_id: int


class AlertDeliveryTestResponse(BaseModel):
    success: bool
    error: str | None = None


def _dedupe_emails(values: list[str]) -> list[str]:
    seen: set[str] = set()
    emails: list[str] = []
    for value in values:
        email = value.strip()
        if not email or email in seen:
            continue
        seen.add(email)
        emails.append(email)
    return emails


class OwnerGroupCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    emails: list[str]
    enabled: bool = True

    @field_validator("emails")
    @classmethod
    def validate_emails(cls, v: list[str]) -> list[str]:
        emails = _dedupe_emails(v)
        if not emails:
            raise ValueError("emails must include at least one address")
        return emails


class OwnerGroupUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=100)
    emails: list[str] | None = None
    enabled: bool | None = None

    @field_validator("emails")
    @classmethod
    def validate_emails(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return None
        emails = _dedupe_emails(v)
        if not emails:
            raise ValueError("emails must include at least one address")
        return emails


class OwnerGroupResponse(BaseModel):
    id: int
    name: str
    emails: list[str]
    enabled: bool
    created_at: datetime | None = None
    updated_at: datetime | None = None


class ResourceOwnerUpsert(BaseModel):
    owner_group_id: int


class ResourceOwnerResponse(BaseModel):
    resource_type: str
    resource_id: str
    display_name: str
    owner_group_id: int | None = None
    owner_group_name: str | None = None


class RuleChannelMapping(BaseModel):
    channel_id: int
    recipients: list[str]


class AlertRuleCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    type: str = Field(..., pattern=r"^(db_health|upstream_health|error_rate|route_error_rate)$")
    target: str = Field(..., min_length=1, max_length=100)
    threshold: float | None = Field(None, ge=0, le=100)
    enabled: bool = True
    channels: list[RuleChannelMapping] = Field(default_factory=list)


class AlertRuleUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=100)
    type: str | None = Field(None, pattern=r"^(db_health|upstream_health|error_rate|route_error_rate)$")
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


class AlertRuleTestChannelResult(BaseModel):
    channel_id: int
    channel_name: str
    recipients: list[str]
    skipped: bool = False
    success: bool | None = None
    error: str | None = None


class AlertRuleTestResponse(BaseModel):
    results: list[AlertRuleTestChannelResult]


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
            return _validate_s3_endpoint_url(v)
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
            return _validate_s3_endpoint_url(v)
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
