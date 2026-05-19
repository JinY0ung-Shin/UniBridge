"""Add saved query templates.

Revision ID: 0006_query_templates
Revises: 0005_alert_trigger_after_failures
Create Date: 2026-05-19
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0006_query_templates"
down_revision = "0005_alert_trigger_after_failures"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "query_templates",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("path", sa.String(length=200), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("description", sa.String(length=255), nullable=True),
        sa.Column("db_alias", sa.String(), nullable=False),
        sa.Column("sql", sa.Text(), nullable=False),
        sa.Column("default_limit", sa.Integer(), nullable=True),
        sa.Column("timeout", sa.Integer(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_query_templates_path",
        "query_templates",
        ["path"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("ix_query_templates_path", table_name="query_templates")
    op.drop_table("query_templates")
