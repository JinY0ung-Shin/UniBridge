from datetime import datetime
from pathlib import PurePosixPath
import re
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
    timeout: int | None = Field(None, ge=1, le=300, description="Query timeout in seconds")


class QueryResponse(BaseModel):
    columns: list[str]
    rows: list[list[Any]]
    row_count: int
    truncated: bool
    elapsed_ms: int
    graph: str | None = Field(
        default=None,
        description=(
            "Set when the underlying engine returned a graph (e.g., SPARQL "
            "CONSTRUCT/DESCRIBE). When set, columns/rows are empty and "
            "row_count is 0 — they do not apply to graph results."
        ),
    )


_QUERY_TEMPLATE_PATH_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def normalize_query_template_path(value: str) -> str:
    path = value.strip().strip("/")
    if not path:
        raise ValueError("Template path must not be empty")
    if len(path) > 200:
        raise ValueError("Template path must be 200 characters or fewer")

    segments = path.split("/")
    if any(segment in {"", ".", ".."} for segment in segments):
        raise ValueError("Template path must not contain empty, '.', or '..' segments")
    if any(_QUERY_TEMPLATE_PATH_SEGMENT_RE.fullmatch(segment) is None for segment in segments):
        raise ValueError("Template path segments may only contain letters, digits, '.', '_', and '-'")
    return path


class QueryTemplateCreate(BaseModel):
    path: str = Field(..., min_length=1, max_length=200)
    name: str = Field(..., min_length=1, max_length=100)
    description: str = Field("", max_length=255)
    database: str = Field(..., min_length=1, description="Database alias to run the template against")
    sql: str = Field(..., min_length=1, description="Read-only SQL/Cypher template using named bind parameters")
    default_limit: int | None = Field(None, ge=1)
    timeout: int | None = Field(None, ge=1, le=300)
    enabled: bool = True

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return normalize_query_template_path(value)

    @field_validator("name", "description", "database", "sql")
    @classmethod
    def strip_text(cls, value: str) -> str:
        return value.strip()


class QueryTemplateUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=100)
    description: str | None = Field(None, max_length=255)
    database: str | None = Field(None, min_length=1)
    sql: str | None = Field(None, min_length=1)
    default_limit: int | None = Field(None, ge=1)
    timeout: int | None = Field(None, ge=1, le=300)
    enabled: bool | None = None

    @field_validator("name", "description", "database", "sql")
    @classmethod
    def strip_optional_text(cls, value: str | None) -> str | None:
        return value.strip() if value is not None else None


class QueryTemplateResponse(BaseModel):
    id: int
    path: str
    name: str
    description: str
    database: str
    sql: str
    default_limit: int | None = None
    timeout: int | None = None
    enabled: bool
    created_at: datetime | None = None
    updated_at: datetime | None = None


class QueryTemplateExecuteRequest(BaseModel):
    params: dict[str, Any] | None = Field(None, description="Named bind parameters for the stored query")
    limit: int | None = Field(None, ge=1, description="Override the template default row limit")
    timeout: int | None = Field(None, ge=1, le=300, description="Override the template default timeout")


# ── DB Connections ───────────────────────────────────────────────────────────

class DBConnectionCreate(BaseModel):
    alias: str = Field(..., min_length=1, max_length=100)
    db_type: str = Field(..., pattern=r"^(postgres|mssql|clickhouse|neo4j|graphdb)$")
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


class QueryHistoryResponse(BaseModel):
    """Page of the current user's own query executions, plus total for paging."""

    items: list[AuditLogResponse]
    total: int


class AdminAuditLogResponse(BaseModel):
    id: int
    timestamp: datetime | None = None
    actor: str
    action: str
    resource_type: str
    resource_id: str
    summary: str | None = None
    before: str | None = None
    after: str | None = None
    status: str
    error_message: str | None = None

    model_config = {"from_attributes": True}


# ── Saved Queries ────────────────────────────────────────────────────────────

# Generous ceiling for a hand-written playground query (~100 KB).
MAX_SAVED_QUERY_SQL_LENGTH = 100_000


class SavedQueryCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    database_alias: str | None = Field(None, max_length=100)
    sql_text: str = Field(..., min_length=1, max_length=MAX_SAVED_QUERY_SQL_LENGTH)
    description: str = Field("", max_length=255)

    @field_validator("name")
    @classmethod
    def strip_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("name must not be empty")
        return value

    @field_validator("sql_text")
    @classmethod
    def require_sql_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("sql_text must not be empty")
        return value

    @field_validator("description")
    @classmethod
    def strip_description(cls, value: str) -> str:
        return value.strip()


class SavedQueryUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=200)
    database_alias: str | None = Field(None, max_length=100)
    sql_text: str | None = Field(None, min_length=1, max_length=MAX_SAVED_QUERY_SQL_LENGTH)
    description: str | None = Field(None, max_length=255)

    @field_validator("name")
    @classmethod
    def strip_optional_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("name must not be empty")
        return value

    @field_validator("sql_text")
    @classmethod
    def require_optional_sql_text(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("sql_text must not be empty")
        return value

    @field_validator("description")
    @classmethod
    def strip_optional_description(cls, value: str | None) -> str | None:
        return value.strip() if value is not None else None


class SavedQueryResponse(BaseModel):
    id: int
    name: str
    database_alias: str | None = None
    sql_text: str
    description: str
    created_at: datetime | None = None
    updated_at: datetime | None = None

    model_config = {"from_attributes": True}


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
    role: str = "user"


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


# ── API Keys ────────────────────────────────────────────────────────────────

class ApiKeyCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100, description="Unique key name (becomes APISIX consumer username)")
    description: str = ""
    api_key: str | None = Field(None, description="Custom API key value; auto-generated if omitted")
    is_master: bool = Field(False, description="Grant all current and future data sources and routes")
    allowed_databases: list[str] = Field(default_factory=list, description="Database aliases this key can query")
    allowed_routes: list[str] = Field(default_factory=list, description="Gateway route IDs this key can access")
    rate_limit_per_minute: int | None = Field(None, ge=1, le=100000, description="Per-minute request cap; null = unlimited")
    allow_insert: bool = Field(False, description="Allow INSERT statements via /query/execute")
    allow_update: bool = Field(False, description="Allow UPDATE statements via /query/execute")
    allow_delete: bool = Field(False, description="Allow DELETE statements via /query/execute")
    allowed_tables: list[str] | None = Field(None, description="Table whitelist for queries; null = all tables")


class ApiKeyUpdate(BaseModel):
    description: str | None = None
    api_key: str | None = Field(None, description="New API key; omit to keep current")
    is_master: bool | None = None
    allowed_databases: list[str] | None = None
    allowed_routes: list[str] | None = None
    rate_limit_per_minute: int | None = Field(None, ge=1, le=100000)
    allow_insert: bool | None = None
    allow_update: bool | None = None
    allow_delete: bool | None = None
    allowed_tables: list[str] | None = Field(None, description="Table whitelist; explicit null clears the restriction")


class ApiKeyResponse(BaseModel):
    name: str
    description: str
    api_key: str | None = None
    key_created: bool = False
    is_master: bool = False
    allowed_databases: list[str]
    allowed_routes: list[str]
    rate_limit_per_minute: int | None = None
    allow_insert: bool = False
    allow_update: bool = False
    allow_delete: bool = False
    allowed_tables: list[str] | None = None
    owner: str | None = None
    expires_at: datetime | None = None
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


def _validate_nas_base_path(path: str) -> str:
    """Pure-string validation for a NAS base_path (NO filesystem I/O).

    Reachability/existence is checked later at /test. This only enforces the
    static, injection-resistant invariants: absolute POSIX path under one of
    the configured allow-list roots, with no traversal segments.
    """
    # Lazy import to avoid a circular import (config imports nothing from here,
    # but schemas is imported very early in app startup).
    from app.config import settings

    if not path:
        raise ValueError("base_path must not be empty")
    if "\x00" in path:
        raise ValueError("base_path must not contain a NUL byte")
    if "\\" in path:
        raise ValueError("base_path must not contain a backslash")

    pure = PurePosixPath(path)
    if not pure.is_absolute():
        raise ValueError("base_path must be an absolute POSIX path")
    if any(part == ".." for part in pure.parts):
        raise ValueError("base_path must not contain '..' segments")

    # Normalize away '.' and redundant separators without touching the fs.
    normalized = PurePosixPath(*[p for p in pure.parts if p != "."])

    allowed_roots = [r.strip() for r in settings.NAS_ALLOWED_ROOTS.split(",") if r.strip()]
    for root in allowed_roots:
        root_path = PurePosixPath(root)
        if normalized == root_path or root_path in normalized.parents:
            return str(normalized)

    raise ValueError("base_path is not under an allowed NAS root")


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
    admin_emails: list[str] = []
    route_error_threshold_pct: float
    check_interval_seconds: int
    trigger_after_failures: int
    updated_at: datetime | None = None


class AlertSettingsUpdate(BaseModel):
    mail_channel_id: int | None = None
    admin_emails: list[str] | None = None
    route_error_threshold_pct: float | None = Field(None, ge=0, le=100)
    check_interval_seconds: int | None = Field(None, ge=30, le=3600)
    trigger_after_failures: int | None = Field(None, ge=1, le=10)

    @field_validator("admin_emails")
    @classmethod
    def validate_admin_emails(cls, v: list[str] | None) -> list[str] | None:
        # Admins are optional (empty list is valid: no global admins configured).
        return _dedupe_emails(v) if v is not None else None

    @model_validator(mode="after")
    def reject_explicit_numeric_nulls(self) -> "AlertSettingsUpdate":
        for field_name in (
            "route_error_threshold_pct",
            "check_interval_seconds",
            "trigger_after_failures",
        ):
            if field_name in self.model_fields_set and getattr(self, field_name) is None:
                raise ValueError(f"{field_name} cannot be null")
        return self


class RecipientTestRequest(BaseModel):
    """Send a test alert to an arbitrary set of emails via a mail channel."""

    mail_channel_id: int
    emails: list[str]

    @field_validator("emails")
    @classmethod
    def validate_emails(cls, v: list[str]) -> list[str]:
        emails = _dedupe_emails(v)
        if not emails:
            raise ValueError("emails must include at least one address")
        return emails


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


class ResourceOwnerUpsert(BaseModel):
    """Assign 담당자 emails and notification state to a resource."""

    emails: list[str] | None = None
    alerts_enabled: bool | None = None

    @field_validator("emails")
    @classmethod
    def validate_emails(cls, v: list[str] | None) -> list[str] | None:
        return _dedupe_emails(v) if v is not None else None


class ResourceOwnerResponse(BaseModel):
    resource_type: str
    resource_id: str
    display_name: str
    emails: list[str] = []
    alerts_enabled: bool = True


class AlertHistoryResponse(BaseModel):
    id: int
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


# ── NAS Connections ─────────────────────────────────────────────────────────

class NasConnectionCreate(BaseModel):
    alias: str = Field(..., min_length=1, max_length=100)
    base_path: str = Field(..., min_length=1)
    read_only: bool = True
    max_download_bytes: int | None = Field(None, ge=1)
    show_hidden: bool = False
    follow_symlinks: bool = False

    @field_validator("base_path")
    @classmethod
    def check_base_path(cls, v: str) -> str:
        return _validate_nas_base_path(v)

    @field_validator("read_only")
    @classmethod
    def enforce_read_only(cls, v: bool) -> bool:
        if v is not True:
            raise ValueError("NAS connections are read-only; read_only must be true")
        return True


class NasConnectionUpdate(BaseModel):
    base_path: str | None = Field(None, min_length=1)
    max_download_bytes: int | None = Field(None, ge=1)
    show_hidden: bool | None = None
    follow_symlinks: bool | None = None
    # read_only is intentionally NOT updatable (always True)

    @field_validator("base_path")
    @classmethod
    def check_base_path(cls, v: str | None) -> str | None:
        return _validate_nas_base_path(v) if v is not None else None


class NasConnectionResponse(BaseModel):
    alias: str
    base_path: str
    read_only: bool
    max_download_bytes: int | None = None
    show_hidden: bool
    follow_symlinks: bool
    status: str = "unknown"

    model_config = {"from_attributes": True}
