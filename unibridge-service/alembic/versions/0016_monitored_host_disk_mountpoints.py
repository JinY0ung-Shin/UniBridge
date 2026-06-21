"""Add per-host disk mountpoint whitelist.

Revision ID: 0016_monitored_host_disk_mountpoints
Revises: 0015_route_error_min_requests
Create Date: 2026-06-21
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0016_monitored_host_disk_mountpoints"
down_revision = "0015_route_error_min_requests"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("monitored_hosts") as batch_op:
        batch_op.add_column(sa.Column("disk_mountpoints", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("monitored_hosts") as batch_op:
        batch_op.drop_column("disk_mountpoints")
