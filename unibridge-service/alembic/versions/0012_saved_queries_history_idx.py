"""Add saved_queries table and audit_logs indexes for per-user query history.

``saved_queries`` stores user-owned playground snippets (owner = Keycloak
``sub``, matching ``api_key_access.owner``). The ``audit_logs`` indexes back
the new ``GET /query/history`` per-user listing and speed up the existing
admin audit-log filters (user / timestamp / database_alias), which previously
scanned the table.

Revision ID: 0012_saved_queries_history_idx
Revises: 0011_apikey_expiry_write_perms
Create Date: 2026-06-10
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0012_saved_queries_history_idx"
down_revision = "0011_apikey_expiry_write_perms"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "saved_queries",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("owner", sa.String(length=255), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("database_alias", sa.String(), nullable=True),
        sa.Column("sql_text", sa.Text(), nullable=False),
        sa.Column("description", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_saved_queries_owner", "saved_queries", ["owner"])

    op.create_index("ix_audit_logs_user", "audit_logs", ["user"])
    op.create_index("ix_audit_logs_timestamp", "audit_logs", ["timestamp"])
    op.create_index("ix_audit_logs_database_alias", "audit_logs", ["database_alias"])


def downgrade() -> None:
    op.drop_index("ix_audit_logs_database_alias", table_name="audit_logs")
    op.drop_index("ix_audit_logs_timestamp", table_name="audit_logs")
    op.drop_index("ix_audit_logs_user", table_name="audit_logs")
    op.drop_index("ix_saved_queries_owner", table_name="saved_queries")
    op.drop_table("saved_queries")
