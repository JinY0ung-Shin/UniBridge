"""Add alert_mutes table, alert_state.pending_notify, alert_history.rule_type.

``alert_mutes`` holds temporary notification suppressions (per resource, or
global). ``alert_state.pending_notify`` remembers that a "triggered"
notification was withheld by a mute, so the alert still announces once the mute
expires while it is firing. ``alert_history.rule_type`` records which
monitoring rule produced a row (``alert_type`` already holds the transition),
and ``sent_at`` gets an index to back history listing + retention cleanup.

Revision ID: 0022_alert_mutes_retention
Revises: 0021_monitored_host_gpu
Create Date: 2026-08-15
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0022_alert_mutes_retention"
down_revision = "0021_monitored_host_gpu"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "alert_mutes",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("resource_type", sa.String(length=20), nullable=False),
        sa.Column("resource_id", sa.String(length=200), nullable=False, server_default=""),
        sa.Column("muted_until", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_by", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("resource_type", "resource_id", name="uq_alert_mute_type_id"),
    )

    with op.batch_alter_table("alert_state") as batch_op:
        batch_op.add_column(
            sa.Column(
                "pending_notify",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )

    with op.batch_alter_table("alert_history") as batch_op:
        batch_op.add_column(sa.Column("rule_type", sa.String(length=30), nullable=True))
    op.create_index("ix_alert_history_sent_at", "alert_history", ["sent_at"])


def downgrade() -> None:
    op.drop_index("ix_alert_history_sent_at", table_name="alert_history")
    with op.batch_alter_table("alert_history") as batch_op:
        batch_op.drop_column("rule_type")
    with op.batch_alter_table("alert_state") as batch_op:
        batch_op.drop_column("pending_notify")
    op.drop_table("alert_mutes")
