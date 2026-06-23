"""Add display_target to alert_history for human-friendly target labels.

Revision ID: 0017_alert_history_display_target
Revises: 0016_monitored_host_disk_mountpoints
Create Date: 2026-06-22
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0017_alert_history_display_target"
down_revision = "0016_monitored_host_disk_mountpoints"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("alert_history") as batch_op:
        batch_op.add_column(sa.Column("display_target", sa.String(length=200), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("alert_history") as batch_op:
        batch_op.drop_column("display_target")
