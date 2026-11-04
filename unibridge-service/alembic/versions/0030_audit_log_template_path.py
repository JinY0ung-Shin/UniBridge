"""Record which query template an audited query ran through.

``audit_logs.template_path`` is set by POST /query/templates/{path} and NULL for
ad-hoc SQL (and for every row written before this revision). The composite index
serves the per-template recent-run counts on the query templates page.

Revision ID: 0030_audit_log_template_path
Revises: 0029_mcp_remote_user_token
Create Date: 2026-11-04
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0030_audit_log_template_path"
down_revision = "0029_mcp_remote_user_token"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("audit_logs") as batch:
        batch.add_column(sa.Column("template_path", sa.String(length=200), nullable=True))
    op.create_index(
        "ix_audit_logs_template_path_timestamp",
        "audit_logs",
        ["template_path", "timestamp"],
    )


def downgrade() -> None:
    op.drop_index("ix_audit_logs_template_path_timestamp", table_name="audit_logs")
    with op.batch_alter_table("audit_logs") as batch:
        batch.drop_column("template_path")
