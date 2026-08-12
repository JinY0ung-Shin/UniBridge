"""Add optional per-host GPU monitoring (dcgm-exporter) columns + global thresholds.

Revision ID: 0021_monitored_host_gpu
Revises: 0020_s3_connection_allowed_buckets
Create Date: 2026-08-12
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0021_monitored_host_gpu"
down_revision = "0020_s3_connection_allowed_buckets"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("monitored_hosts") as batch_op:
        batch_op.add_column(sa.Column("gpu_address", sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column("gpu_util_warn_pct", sa.Float(), nullable=True))
        batch_op.add_column(sa.Column("gpu_mem_warn_pct", sa.Float(), nullable=True))
    with op.batch_alter_table("alert_settings") as batch_op:
        batch_op.add_column(
            sa.Column(
                "server_gpu_util_warn_pct",
                sa.Float(),
                nullable=False,
                server_default="90.0",
            )
        )
        batch_op.add_column(
            sa.Column(
                "server_gpu_mem_warn_pct",
                sa.Float(),
                nullable=False,
                server_default="90.0",
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("alert_settings") as batch_op:
        batch_op.drop_column("server_gpu_mem_warn_pct")
        batch_op.drop_column("server_gpu_util_warn_pct")
    with op.batch_alter_table("monitored_hosts") as batch_op:
        batch_op.drop_column("gpu_mem_warn_pct")
        batch_op.drop_column("gpu_util_warn_pct")
        batch_op.drop_column("gpu_address")
