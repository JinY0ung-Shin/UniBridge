"""Persist current alert state.

Revision ID: 0003_alert_state
Revises: 0002_permission_role_fk
Create Date: 2026-05-07
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0003_alert_state"
down_revision = "0002_permission_role_fk"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "alert_state",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("alert_type", sa.String(length=30), nullable=False),
        sa.Column("target", sa.String(length=200), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("since", sa.DateTime(timezone=True), nullable=False),
        sa.Column("display_target", sa.String(length=200), nullable=True),
        sa.Column("alert_notified", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("alert_type", "target", name="uq_alert_state_type_target"),
    )


def downgrade() -> None:
    op.drop_table("alert_state")
