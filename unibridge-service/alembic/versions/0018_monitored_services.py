"""Add monitored_services table (external API-service RED-metrics registry).

Revision ID: 0018_monitored_services
Revises: 0017_alert_history_display_target
Create Date: 2026-07-05
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0018_monitored_services"
down_revision = "0017_alert_history_display_target"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "monitored_services",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("address", sa.String(length=255), nullable=False),
        sa.Column("metrics_path", sa.String(length=255), nullable=False, server_default="/metrics"),
        sa.Column("description", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_monitored_services_name", "monitored_services", ["name"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_monitored_services_name", table_name="monitored_services")
    op.drop_table("monitored_services")
