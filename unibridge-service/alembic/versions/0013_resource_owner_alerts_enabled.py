"""Add per-resource alert enable flag.

Revision ID: 0013_resource_owner_alerts_enabled
Revises: 0012_saved_queries_history_idx
Create Date: 2026-06-12
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0013_resource_owner_alerts_enabled"
down_revision = "0012_saved_queries_history_idx"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("resource_owners") as batch_op:
        batch_op.add_column(
            sa.Column("alerts_enabled", sa.Boolean(), nullable=False, server_default=sa.true())
        )


def downgrade() -> None:
    with op.batch_alter_table("resource_owners") as batch_op:
        batch_op.drop_column("alerts_enabled")
