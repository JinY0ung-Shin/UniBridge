"""Add per-connection bucket allow-list to s3_connections.

Revision ID: 0020_s3_connection_allowed_buckets
Revises: 0019_monitored_service_scheme
Create Date: 2026-08-11
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0020_s3_connection_allowed_buckets"
down_revision = "0019_monitored_service_scheme"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("s3_connections") as batch_op:
        batch_op.add_column(sa.Column("allowed_buckets", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("s3_connections") as batch_op:
        batch_op.drop_column("allowed_buckets")
