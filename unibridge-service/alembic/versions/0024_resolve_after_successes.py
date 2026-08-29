"""Add recovery damping: alert_settings.resolve_after_successes, alert_state.success_count.

``resolve_after_successes`` is the recovery-side mirror of
``trigger_after_failures``: an alerting target resolves only after that many
consecutive healthy cycles. The default 5 applies damping out of the box —
recovery is announced only after five consecutive healthy cycles (about five
minutes at the default 60s check interval); set 1 to restore the previous
resolve-on-first-healthy-cycle behaviour. ``alert_state.success_count`` is the
per-target tally backing it, persisted so a restart mid-recovery does not
restart the streak from zero.

Revision ID: 0024_resolve_after_successes
Revises: 0023_gpu_util_target
Create Date: 2026-08-29
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0024_resolve_after_successes"
down_revision = "0023_gpu_util_target"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("alert_settings") as batch_op:
        batch_op.add_column(
            sa.Column(
                "resolve_after_successes",
                sa.Integer(),
                nullable=False,
                server_default="5",
            )
        )
        batch_op.create_check_constraint(
            "ck_alert_settings_resolve_after_successes_range",
            "resolve_after_successes BETWEEN 1 AND 60",
        )

    with op.batch_alter_table("alert_state") as batch_op:
        batch_op.add_column(
            sa.Column(
                "success_count",
                sa.Integer(),
                nullable=False,
                server_default="0",
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("alert_state") as batch_op:
        batch_op.drop_column("success_count")

    with op.batch_alter_table("alert_settings") as batch_op:
        batch_op.drop_constraint(
            "ck_alert_settings_resolve_after_successes_range",
            type_="check",
        )
        batch_op.drop_column("resolve_after_successes")
