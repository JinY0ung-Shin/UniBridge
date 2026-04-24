from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    Column,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase

from app.db_types import UtcDateTime


class Base(DeclarativeBase):
    pass


class DBConnection(Base):
    __tablename__ = "db_connections"

    id = Column(Integer, primary_key=True, autoincrement=True)
    alias = Column(String, unique=True, nullable=False, index=True)
    db_type = Column(String, nullable=False)  # "postgres", "mssql", or "clickhouse"
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
    created_at = Column(UtcDateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(UtcDateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))


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
    created_at = Column(UtcDateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(UtcDateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))


class Permission(Base):
    __tablename__ = "permissions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    role = Column(String, nullable=False)
    db_alias = Column(String, nullable=False)
    allow_select = Column(Boolean, default=True)
    allow_insert = Column(Boolean, default=False)
    allow_update = Column(Boolean, default=False)
    allow_delete = Column(Boolean, default=False)
    allowed_tables = Column(Text, nullable=True)  # JSON array: ["users", "orders"], null = all

    __table_args__ = (UniqueConstraint("role", "db_alias", name="uq_role_db_alias"),)


class ApiKeyAccess(Base):
    __tablename__ = "api_key_access"

    id = Column(Integer, primary_key=True, autoincrement=True)
    consumer_name = Column(String(100), unique=True, nullable=False, index=True)
    description = Column(String(255), default="")
    allowed_databases = Column(Text, nullable=True)  # JSON array: ["mydb", "analytics"], null = none
    allowed_routes = Column(Text, nullable=True)  # JSON array: ["route-id-1", "route-id-2"], null = none
    created_at = Column(UtcDateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(UtcDateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))


class Role(Base):
    __tablename__ = "roles"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(100), unique=True, nullable=False)
    description = Column(String(255), default="")
    is_system = Column(Boolean, default=False)


class RolePermission(Base):
    __tablename__ = "role_permissions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    role_id = Column(Integer, ForeignKey("roles.id", ondelete="CASCADE"), nullable=False)
    permission = Column(String(100), nullable=False)

    __table_args__ = (UniqueConstraint("role_id", "permission", name="uq_role_permission"),)


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    timestamp = Column(UtcDateTime, default=lambda: datetime.now(timezone.utc))
    user = Column(String, nullable=False)
    database_alias = Column(String, nullable=False)
    sql = Column(Text, nullable=False)
    params = Column(Text, nullable=True)  # JSON string
    row_count = Column(Integer, nullable=True)
    elapsed_ms = Column(Integer, nullable=True)
    status = Column(String, nullable=False)  # "success" or "error"
    error_message = Column(Text, nullable=True)


class SystemConfig(Base):
    __tablename__ = "system_config"

    key = Column(String(100), primary_key=True)
    value = Column(Text, nullable=False)
    updated_at = Column(UtcDateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))


class AlertChannel(Base):
    __tablename__ = "alert_channels"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(100), unique=True, nullable=False)
    webhook_url = Column(String, nullable=False)
    payload_template = Column(Text, nullable=False)
    headers = Column(Text, nullable=True)  # JSON object
    enabled = Column(Boolean, default=True, nullable=False, server_default="true")
    created_at = Column(UtcDateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(UtcDateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    def __init__(self, **kwargs):
        kwargs.setdefault("enabled", True)
        super().__init__(**kwargs)


class AlertRule(Base):
    __tablename__ = "alert_rules"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(100), nullable=False)
    type = Column(String(30), nullable=False)  # "db_health", "upstream_health", "error_rate"
    target = Column(String(100), nullable=False)  # DB alias, upstream ID, or "*"
    threshold = Column(Float, nullable=True)  # error rate % (error_rate type only)
    enabled = Column(Boolean, default=True, nullable=False, server_default="true")
    created_at = Column(UtcDateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(UtcDateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    def __init__(self, **kwargs):
        kwargs.setdefault("enabled", True)
        super().__init__(**kwargs)


class AlertRuleChannel(Base):
    __tablename__ = "alert_rule_channels"

    id = Column(Integer, primary_key=True, autoincrement=True)
    rule_id = Column(Integer, ForeignKey("alert_rules.id", ondelete="CASCADE"), nullable=False)
    channel_id = Column(Integer, ForeignKey("alert_channels.id", ondelete="CASCADE"), nullable=False)
    recipients = Column(Text, nullable=False)  # JSON array: ["user@example.com"]


class AlertHistory(Base):
    __tablename__ = "alert_history"

    id = Column(Integer, primary_key=True, autoincrement=True)
    rule_id = Column(Integer, ForeignKey("alert_rules.id", ondelete="SET NULL"), nullable=True)
    channel_id = Column(Integer, ForeignKey("alert_channels.id", ondelete="SET NULL"), nullable=True)
    alert_type = Column(String(20), nullable=False)  # "triggered" / "resolved"
    target = Column(String(100), nullable=False)
    message = Column(Text, nullable=False)
    recipients = Column(Text, nullable=True)  # JSON array
    sent_at = Column(UtcDateTime, default=lambda: datetime.now(timezone.utc))
    success = Column(Boolean, nullable=True)
    error_detail = Column(Text, nullable=True)
