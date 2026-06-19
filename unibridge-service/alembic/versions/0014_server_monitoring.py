"""Add monitored_hosts table, server alert settings, and alert severity.

Revision ID: 0014_server_monitoring
Revises: 0013_resource_owner_alerts_enabled
Create Date: 2026-06-19
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0014_server_monitoring"
down_revision = "0013_resource_owner_alerts_enabled"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "monitored_hosts",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("address", sa.String(length=255), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("labels", sa.Text(), nullable=True),
        sa.Column("description", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("disk_warn_pct", sa.Float(), nullable=True),
        sa.Column("disk_crit_pct", sa.Float(), nullable=True),
        sa.Column("cpu_warn_pct", sa.Float(), nullable=True),
        sa.Column("mem_warn_pct", sa.Float(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_monitored_hosts_name", "monitored_hosts", ["name"], unique=True)

    with op.batch_alter_table("alert_settings") as batch_op:
        batch_op.add_column(sa.Column("server_disk_warn_pct", sa.Float(), nullable=False, server_default="80.0"))
        batch_op.add_column(sa.Column("server_disk_crit_pct", sa.Float(), nullable=False, server_default="90.0"))
        batch_op.add_column(sa.Column("server_cpu_warn_pct", sa.Float(), nullable=False, server_default="90.0"))
        batch_op.add_column(sa.Column("server_mem_warn_pct", sa.Float(), nullable=False, server_default="90.0"))
        batch_op.add_column(sa.Column("server_disk_forecast_hours", sa.Float(), nullable=False, server_default="24.0"))
        batch_op.add_column(sa.Column("repeat_alert_after_cycles", sa.Integer(), nullable=False, server_default="0"))

    with op.batch_alter_table("alert_state") as batch_op:
        batch_op.add_column(sa.Column("severity", sa.String(length=20), nullable=True))

    with op.batch_alter_table("alert_history") as batch_op:
        batch_op.add_column(sa.Column("severity", sa.String(length=20), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("alert_history") as batch_op:
        batch_op.drop_column("severity")

    with op.batch_alter_table("alert_state") as batch_op:
        batch_op.drop_column("severity")

    with op.batch_alter_table("alert_settings") as batch_op:
        batch_op.drop_column("repeat_alert_after_cycles")
        batch_op.drop_column("server_disk_forecast_hours")
        batch_op.drop_column("server_mem_warn_pct")
        batch_op.drop_column("server_cpu_warn_pct")
        batch_op.drop_column("server_disk_crit_pct")
        batch_op.drop_column("server_disk_warn_pct")

    op.drop_index("ix_monitored_hosts_name", table_name="monitored_hosts")
    op.drop_table("monitored_hosts")
