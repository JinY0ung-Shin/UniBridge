"""Add admin_audit_logs table for administrative change auditing.

Records mutations to managed resources (gateway routes/upstreams, API keys)
with actor, action, before/after snapshots (secrets redacted) — distinct from
the query-execution ``audit_logs`` table.

Revision ID: 0010_admin_audit_log
Revises: 0009_alert_simplify_recipients
Create Date: 2026-06-10
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0010_admin_audit_log"
down_revision = "0009_alert_simplify_recipients"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "admin_audit_logs",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=True),
        sa.Column("actor", sa.String(), nullable=False),
        sa.Column("action", sa.String(), nullable=False),
        sa.Column("resource_type", sa.String(), nullable=False),
        sa.Column("resource_id", sa.String(), nullable=False),
        sa.Column("summary", sa.String(), nullable=True),
        sa.Column("before", sa.Text(), nullable=True),
        sa.Column("after", sa.Text(), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
    )
    op.create_index("ix_admin_audit_logs_timestamp", "admin_audit_logs", ["timestamp"])
    op.create_index("ix_admin_audit_logs_actor", "admin_audit_logs", ["actor"])
    op.create_index(
        "ix_admin_audit_logs_resource_type", "admin_audit_logs", ["resource_type"]
    )


def downgrade() -> None:
    op.drop_index("ix_admin_audit_logs_resource_type", table_name="admin_audit_logs")
    op.drop_index("ix_admin_audit_logs_actor", table_name="admin_audit_logs")
    op.drop_index("ix_admin_audit_logs_timestamp", table_name="admin_audit_logs")
    op.drop_table("admin_audit_logs")
