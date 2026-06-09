from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, relationship

from app.db_types import UtcDateTime, utcnow


class Base(DeclarativeBase):
    pass


class DBConnection(Base):
    __tablename__ = "db_connections"

    id = Column(Integer, primary_key=True, autoincrement=True)
    alias = Column(String, unique=True, nullable=False, index=True)
    db_type = Column(String, nullable=False)  # "postgres", "mssql", "clickhouse", "neo4j", or "graphdb"
    host = Column(String, nullable=False)
    port = Column(Integer, nullable=False)
    database = Column(String, nullable=False)
    username = Column(String, nullable=False)
    password_encrypted = Column(String, nullable=False)
    protocol = Column(String(16), nullable=True)
    secure = Column(Boolean, nullable=True)
    pool_size = Column(Integer, default=5)
    max_overflow = Column(Integer, default=3)
    query_timeout = Column(Integer, default=30)
    created_at = Column(UtcDateTime, default=utcnow)
    updated_at = Column(UtcDateTime, default=utcnow, onupdate=utcnow)


class S3Connection(Base):
    __tablename__ = "s3_connections"

    id = Column(Integer, primary_key=True, autoincrement=True)
    alias = Column(String, unique=True, nullable=False, index=True)
    endpoint_url = Column(String, nullable=True)
    region = Column(String, nullable=False, default="us-east-1")
    access_key_id_encrypted = Column(String, nullable=False)
    secret_access_key_encrypted = Column(String, nullable=False)
    default_bucket = Column(String, nullable=True)
    use_ssl = Column(Boolean, default=True)
    created_at = Column(UtcDateTime, default=utcnow)
    updated_at = Column(UtcDateTime, default=utcnow, onupdate=utcnow)


class NASConnection(Base):
    __tablename__ = "nas_connections"

    id = Column(Integer, primary_key=True, autoincrement=True)
    alias = Column(String, unique=True, nullable=False, index=True)
    base_path = Column(String, nullable=False)
    read_only = Column(Boolean, default=True)
    max_download_bytes = Column(Integer, nullable=True)   # per-connection cap; may only LOWER the global ceiling
    show_hidden = Column(Boolean, default=False)
    follow_symlinks = Column(Boolean, default=False)      # per-connection, NOT a global
    created_at = Column(UtcDateTime, default=utcnow)
    updated_at = Column(UtcDateTime, default=utcnow, onupdate=utcnow)


class Permission(Base):
    __tablename__ = "permissions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    role = Column(
        String(100),
        ForeignKey("roles.name", ondelete="CASCADE", onupdate="CASCADE"),
        nullable=False,
    )
    db_alias = Column(String, nullable=False)
    allow_select = Column(Boolean, default=True)
    allow_insert = Column(Boolean, default=False)
    allow_update = Column(Boolean, default=False)
    allow_delete = Column(Boolean, default=False)
    allowed_tables = Column(Text, nullable=True)  # JSON array: ["users", "orders"], null = all

    __table_args__ = (UniqueConstraint("role", "db_alias", name="uq_role_db_alias"),)

    role_ref = relationship("Role", back_populates="db_permissions", foreign_keys=[role])


class QueryTemplate(Base):
    __tablename__ = "query_templates"

    id = Column(Integer, primary_key=True, autoincrement=True)
    path = Column(String(200), unique=True, nullable=False, index=True)
    name = Column(String(100), nullable=False)
    description = Column(String(255), default="")
    db_alias = Column(String, nullable=False)
    sql = Column(Text, nullable=False)
    default_limit = Column(Integer, nullable=True)
    timeout = Column(Integer, nullable=True)
    enabled = Column(Boolean, default=True, nullable=False, server_default="true")
    created_at = Column(UtcDateTime, default=utcnow)
    updated_at = Column(UtcDateTime, default=utcnow, onupdate=utcnow)

    def __init__(self, **kwargs):
        kwargs.setdefault("enabled", True)
        super().__init__(**kwargs)


class ApiKeyAccess(Base):
    __tablename__ = "api_key_access"

    id = Column(Integer, primary_key=True, autoincrement=True)
    consumer_name = Column(String(100), unique=True, nullable=False, index=True)
    description = Column(String(255), default="")
    allowed_databases = Column(Text, nullable=True)  # JSON array: ["mydb", "analytics"], null = none
    allowed_routes = Column(Text, nullable=True)  # JSON array: ["route-id-1", "route-id-2"], null = none
    owner = Column(String(255), nullable=True, index=True)  # Keycloak sub; NULL = admin/shared key
    rate_limit_per_minute = Column(Integer, nullable=True)  # NULL = unlimited
    created_at = Column(UtcDateTime, default=utcnow)
    updated_at = Column(UtcDateTime, default=utcnow, onupdate=utcnow)


class Role(Base):
    __tablename__ = "roles"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(100), unique=True, nullable=False)
    description = Column(String(255), default="")
    is_system = Column(Boolean, default=False)

    db_permissions = relationship(
        "Permission",
        back_populates="role_ref",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class RolePermission(Base):
    __tablename__ = "role_permissions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    role_id = Column(Integer, ForeignKey("roles.id", ondelete="CASCADE"), nullable=False)
    permission = Column(String(100), nullable=False)

    __table_args__ = (UniqueConstraint("role_id", "permission", name="uq_role_permission"),)


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    timestamp = Column(UtcDateTime, default=utcnow)
    user = Column(String, nullable=False)
    database_alias = Column(String, nullable=False)
    sql = Column(Text, nullable=False)
    params = Column(Text, nullable=True)  # JSON string
    row_count = Column(Integer, nullable=True)
    elapsed_ms = Column(Integer, nullable=True)
    status = Column(String, nullable=False)  # "success" or "error"
    error_message = Column(Text, nullable=True)


class AdminAuditLog(Base):
    """Audit trail for administrative configuration changes.

    Unlike :class:`AuditLog` (which records SQL/SPARQL query execution), this
    records mutations to managed resources — gateway routes/upstreams and API
    keys — capturing who changed what, and before/after snapshots (with secrets
    redacted) so a change can be reviewed or reverted.
    """

    __tablename__ = "admin_audit_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    timestamp = Column(UtcDateTime, default=utcnow, index=True)
    actor = Column(String, nullable=False, index=True)  # username that made the change
    action = Column(String, nullable=False)  # "create" | "update" | "delete"
    resource_type = Column(String, nullable=False, index=True)  # "route" | "upstream" | "api_key"
    resource_id = Column(String, nullable=False)  # route_id / upstream_id / consumer name
    summary = Column(String, nullable=True)  # quick-scan label, e.g. route uri
    before = Column(Text, nullable=True)  # JSON snapshot before change (None for create)
    after = Column(Text, nullable=True)  # JSON snapshot after change (None for delete)
    status = Column(String, nullable=False)  # "success" or "error"
    error_message = Column(Text, nullable=True)


class SystemConfig(Base):
    __tablename__ = "system_config"

    key = Column(String(100), primary_key=True)
    value = Column(Text, nullable=False)
    updated_at = Column(UtcDateTime, default=utcnow, onupdate=utcnow)


class AlertChannel(Base):
    __tablename__ = "alert_channels"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(100), unique=True, nullable=False)
    webhook_url = Column(String, nullable=False)
    payload_template = Column(Text, nullable=False)
    recipient_item_template = Column(Text, nullable=True)
    headers = Column(Text, nullable=True)  # JSON object
    enabled = Column(Boolean, default=True, nullable=False, server_default="true")
    created_at = Column(UtcDateTime, default=utcnow)
    updated_at = Column(UtcDateTime, default=utcnow, onupdate=utcnow)

    def __init__(self, **kwargs):
        kwargs.setdefault("enabled", True)
        super().__init__(**kwargs)


class ResourceOwner(Base):
    """Per-resource assignees (담당자).

    Holds the alert-recipient emails for a single resource directly — no
    intermediary owner-group. ``emails`` is a JSON array of strings.
    """

    __tablename__ = "resource_owners"

    id = Column(Integer, primary_key=True, autoincrement=True)
    resource_type = Column(String(20), nullable=False)
    resource_id = Column(String(200), nullable=False)
    emails = Column(Text, nullable=False)  # JSON array of assignee emails
    created_at = Column(UtcDateTime, default=utcnow, nullable=False)
    updated_at = Column(UtcDateTime, default=utcnow, onupdate=utcnow, nullable=False)

    __table_args__ = (
        UniqueConstraint("resource_type", "resource_id", name="uq_resource_owner_type_id"),
    )


class AlertSettings(Base):
    __tablename__ = "alert_settings"

    id = Column(Integer, primary_key=True)
    mail_channel_id = Column(Integer, ForeignKey("alert_channels.id", ondelete="RESTRICT"), nullable=True)
    # Global admins (관리자) — receive every alert. JSON array of emails.
    admin_emails = Column(Text, default="[]", server_default="[]", nullable=False)
    route_error_threshold_pct = Column(Float, default=10.0, nullable=False, server_default="10.0")
    check_interval_seconds = Column(Integer, default=60, nullable=False, server_default="60")
    trigger_after_failures = Column(
        Integer,
        default=2,
        nullable=False,
        server_default="2",
    )
    updated_at = Column(UtcDateTime, default=utcnow, onupdate=utcnow, nullable=False)

    __table_args__ = (
        CheckConstraint("id = 1", name="ck_alert_settings_singleton"),
        CheckConstraint(
            "trigger_after_failures BETWEEN 1 AND 10",
            name="ck_alert_settings_trigger_after_failures_range",
        ),
    )


class AlertHistory(Base):
    __tablename__ = "alert_history"

    id = Column(Integer, primary_key=True, autoincrement=True)
    channel_id = Column(Integer, ForeignKey("alert_channels.id", ondelete="SET NULL"), nullable=True)
    resource_type = Column(String(20), nullable=True)
    alert_type = Column(String(20), nullable=False)  # "triggered" / "resolved"
    target = Column(String(100), nullable=False)
    message = Column(Text, nullable=False)
    recipients = Column(Text, nullable=True)  # JSON array
    sent_at = Column(UtcDateTime, default=utcnow)
    success = Column(Boolean, nullable=True)
    error_detail = Column(Text, nullable=True)


class AlertState(Base):
    __tablename__ = "alert_state"

    id = Column(Integer, primary_key=True, autoincrement=True)
    alert_type = Column(String(30), nullable=False)
    target = Column(String(200), nullable=False)
    status = Column(String(20), nullable=False)
    since = Column(UtcDateTime, default=utcnow, nullable=False)
    display_target = Column(String(200), nullable=True)
    fail_count = Column(Integer, default=0, nullable=False, server_default="0")
    updated_at = Column(UtcDateTime, default=utcnow, onupdate=utcnow)

    __table_args__ = (
        UniqueConstraint("alert_type", "target", name="uq_alert_state_type_target"),
    )
