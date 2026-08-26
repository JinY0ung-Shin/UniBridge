"""Add GPU utilisation targets for the daily under-utilisation report.

``monitored_hosts.gpu_util_target_pct`` is the per-host target (null = inherit
the global default, 0 = report off for that host);
``alert_settings.server_gpu_util_target_pct`` is that global default (0 = the
report is off everywhere). ``alert_settings.server_gpu_report_last_sent_at``
is the once-per-day marker: the UTC timestamp of the last completed run, kept
in the shared meta DB so blue/green colors and restarts cannot double-send.

Revision ID: 0023_gpu_util_target
Revises: 0022_alert_mutes_retention
Create Date: 2026-08-26
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0023_gpu_util_target"
down_revision = "0022_alert_mutes_retention"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("monitored_hosts") as batch_op:
        batch_op.add_column(sa.Column("gpu_util_target_pct", sa.Float(), nullable=True))
    with op.batch_alter_table("alert_settings") as batch_op:
        batch_op.add_column(
            sa.Column(
                "server_gpu_util_target_pct",
                sa.Float(),
                nullable=False,
                server_default="0.0",
            )
        )
        batch_op.add_column(
            sa.Column(
                "server_gpu_report_last_sent_at",
                sa.DateTime(timezone=True),
                nullable=True,
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("alert_settings") as batch_op:
        batch_op.drop_column("server_gpu_report_last_sent_at")
        batch_op.drop_column("server_gpu_util_target_pct")
    with op.batch_alter_table("monitored_hosts") as batch_op:
        batch_op.drop_column("gpu_util_target_pct")
