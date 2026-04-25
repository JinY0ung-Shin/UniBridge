"""Initial meta database schema.

Revision ID: 0001_initial
Revises:
Create Date: 2026-04-25
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "roles",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("description", sa.String(length=255), nullable=True),
        sa.Column("is_system", sa.Boolean(), nullable=True),
        sa.UniqueConstraint("name"),
    )
    op.create_table(
        "db_connections",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("alias", sa.String(), nullable=False),
        sa.Column("db_type", sa.String(), nullable=False),
        sa.Column("host", sa.String(), nullable=False),
        sa.Column("port", sa.Integer(), nullable=False),
        sa.Column("database", sa.String(), nullable=False),
        sa.Column("username", sa.String(), nullable=False),
        sa.Column("password_encrypted", sa.String(), nullable=False),
        sa.Column("protocol", sa.String(length=16), nullable=True),
        sa.Column("secure", sa.Boolean(), nullable=True),
        sa.Column("pool_size", sa.Integer(), nullable=True),
        sa.Column("max_overflow", sa.Integer(), nullable=True),
        sa.Column("query_timeout", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_db_connections_alias", "db_connections", ["alias"], unique=True)
    op.create_table(
        "s3_connections",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("alias", sa.String(), nullable=False),
        sa.Column("endpoint_url", sa.String(), nullable=True),
        sa.Column("region", sa.String(), nullable=False),
        sa.Column("access_key_id_encrypted", sa.String(), nullable=False),
        sa.Column("secret_access_key_encrypted", sa.String(), nullable=False),
        sa.Column("default_bucket", sa.String(), nullable=True),
        sa.Column("use_ssl", sa.Boolean(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_s3_connections_alias", "s3_connections", ["alias"], unique=True)
    op.create_table(
        "permissions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("role", sa.String(length=100), nullable=False),
        sa.Column("db_alias", sa.String(), nullable=False),
        sa.Column("allow_select", sa.Boolean(), nullable=True),
        sa.Column("allow_insert", sa.Boolean(), nullable=True),
        sa.Column("allow_update", sa.Boolean(), nullable=True),
        sa.Column("allow_delete", sa.Boolean(), nullable=True),
        sa.Column("allowed_tables", sa.Text(), nullable=True),
        sa.UniqueConstraint("role", "db_alias", name="uq_role_db_alias"),
    )
    op.create_table(
        "api_key_access",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("consumer_name", sa.String(length=100), nullable=False),
        sa.Column("description", sa.String(length=255), nullable=True),
        sa.Column("allowed_databases", sa.Text(), nullable=True),
        sa.Column("allowed_routes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_api_key_access_consumer_name", "api_key_access", ["consumer_name"], unique=True)
    op.create_table(
        "role_permissions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("role_id", sa.Integer(), nullable=False),
        sa.Column("permission", sa.String(length=100), nullable=False),
        sa.ForeignKeyConstraint(["role_id"], ["roles.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("role_id", "permission", name="uq_role_permission"),
    )
    op.create_table(
        "audit_logs",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=True),
        sa.Column("user", sa.String(), nullable=False),
        sa.Column("database_alias", sa.String(), nullable=False),
        sa.Column("sql", sa.Text(), nullable=False),
        sa.Column("params", sa.Text(), nullable=True),
        sa.Column("row_count", sa.Integer(), nullable=True),
        sa.Column("elapsed_ms", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
    )
    op.create_table(
        "system_config",
        sa.Column("key", sa.String(length=100), primary_key=True),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_table(
        "alert_channels",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("webhook_url", sa.String(), nullable=False),
        sa.Column("payload_template", sa.Text(), nullable=False),
        sa.Column("headers", sa.Text(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("name"),
    )
    op.create_table(
        "alert_rules",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("type", sa.String(length=30), nullable=False),
        sa.Column("target", sa.String(length=100), nullable=False),
        sa.Column("threshold", sa.Float(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_table(
        "alert_rule_channels",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("rule_id", sa.Integer(), nullable=False),
        sa.Column("channel_id", sa.Integer(), nullable=False),
        sa.Column("recipients", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["rule_id"], ["alert_rules.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["channel_id"], ["alert_channels.id"], ondelete="CASCADE"),
    )
    op.create_table(
        "alert_history",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("rule_id", sa.Integer(), nullable=True),
        sa.Column("channel_id", sa.Integer(), nullable=True),
        sa.Column("alert_type", sa.String(length=20), nullable=False),
        sa.Column("target", sa.String(length=100), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("recipients", sa.Text(), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("success", sa.Boolean(), nullable=True),
        sa.Column("error_detail", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["rule_id"], ["alert_rules.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["channel_id"], ["alert_channels.id"], ondelete="SET NULL"),
    )


def downgrade() -> None:
    op.drop_table("alert_history")
    op.drop_table("alert_rule_channels")
    op.drop_table("alert_rules")
    op.drop_table("alert_channels")
    op.drop_table("system_config")
    op.drop_table("audit_logs")
    op.drop_table("role_permissions")
    op.drop_index("ix_api_key_access_consumer_name", table_name="api_key_access")
    op.drop_table("api_key_access")
    op.drop_table("permissions")
    op.drop_index("ix_s3_connections_alias", table_name="s3_connections")
    op.drop_table("s3_connections")
    op.drop_index("ix_db_connections_alias", table_name="db_connections")
    op.drop_table("db_connections")
    op.drop_table("roles")
