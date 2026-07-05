"""Add scheme column to monitored_services (per-service http/https scrape).

Revision ID: 0019_monitored_service_scheme
Revises: 0018_monitored_services
Create Date: 2026-07-05
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0019_monitored_service_scheme"
down_revision = "0018_monitored_services"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("monitored_services") as batch_op:
        batch_op.add_column(
            sa.Column("scheme", sa.String(length=8), nullable=False, server_default="http")
        )


def downgrade() -> None:
    with op.batch_alter_table("monitored_services") as batch_op:
        batch_op.drop_column("scheme")
