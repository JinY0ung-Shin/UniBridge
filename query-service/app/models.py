from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


class DBConnection(Base):
    __tablename__ = "db_connections"

    id = Column(Integer, primary_key=True, autoincrement=True)
    alias = Column(String, unique=True, nullable=False, index=True)
    db_type = Column(String, nullable=False)  # "postgres" or "mssql"
    host = Column(String, nullable=False)
    port = Column(Integer, nullable=False)
    database = Column(String, nullable=False)
    username = Column(String, nullable=False)
    password_encrypted = Column(String, nullable=False)
    pool_size = Column(Integer, default=5)
    max_overflow = Column(Integer, default=3)
    query_timeout = Column(Integer, default=30)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())


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
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())


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
    timestamp = Column(DateTime, server_default=func.now())
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
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())
