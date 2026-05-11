"""Add trigger_after_failures and replace alert_notified with fail_count.

Revision ID: 0005_alert_trigger_after_failures
Revises: 0004_alert_owner_routing
Create Date: 2026-05-11

Upgrade collapses the legacy two-state distinction (alert+notified vs
alert+unnotified) into a single counter, which is intentionally lossy:
the pre-notification state (alert, alert_notified=FALSE) is rewritten to
(ok, fail_count=N-1). Downgrade cannot reconstruct the original row from
that compacted shape — (ok, fail_count>0) is also a legitimate steady
state under the new model — so the downgrade restores alert_notified
conservatively (TRUE for ok rows, matching the old default).
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0005_alert_trigger_after_failures"
down_revision = "0004_alert_owner_routing"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("alert_settings") as batch_op:
        batch_op.add_column(
            sa.Column(
                "trigger_after_failures",
                sa.Integer(),
                nullable=False,
                server_default="2",
            )
        )
        batch_op.create_check_constraint(
            "ck_alert_settings_trigger_after_failures_range",
            "trigger_after_failures BETWEEN 1 AND 10",
        )

    with op.batch_alter_table("alert_state") as batch_op:
        batch_op.add_column(
            sa.Column(
                "fail_count",
                sa.Integer(),
                nullable=False,
                server_default="0",
            )
        )

    # Backfill fail_count from the legacy (status, alert_notified) pair using the
    # configured N. If alert_settings is empty (fresh deploy mid-migration), fall
    # back to N=2 — the new column's server default.
    op.execute(sa.text(
        """
        UPDATE alert_state
        SET fail_count = CASE
            WHEN status = 'alert' AND alert_notified = TRUE THEN
                COALESCE((SELECT trigger_after_failures FROM alert_settings WHERE id = 1), 2)
            WHEN status = 'alert' AND alert_notified = FALSE THEN
                CASE
                    WHEN COALESCE((SELECT trigger_after_failures FROM alert_settings WHERE id = 1), 2) - 1 < 0
                        THEN 0
                    ELSE COALESCE((SELECT trigger_after_failures FROM alert_settings WHERE id = 1), 2) - 1
                END
            ELSE 0
        END
        """
    ))

    # Tracked-but-unnotified alerts: under the new model they live as ok with
    # accumulated fail_count, awaiting one more failure to fire.
    op.execute(sa.text(
        """
        UPDATE alert_state
        SET status = 'ok',
            since = COALESCE(updated_at, since)
        WHERE status = 'alert' AND alert_notified = FALSE
        """
    ))

    with op.batch_alter_table("alert_state") as batch_op:
        batch_op.drop_column("alert_notified")


def downgrade() -> None:
    with op.batch_alter_table("alert_state") as batch_op:
        batch_op.add_column(
            sa.Column(
                "alert_notified",
                sa.Boolean(),
                nullable=False,
                server_default=sa.true(),
            )
        )

    # Best-effort restore: alert rows recover their notified flag from
    # fail_count; ok rows always become alert_notified=TRUE because the
    # upgrade's collapse of (alert, FALSE) → (ok, fail_count=N-1) is
    # one-way (see module docstring). Rolling forward and back on a row
    # that started as (alert, FALSE) yields (ok, TRUE), not the original.
    op.execute(sa.text(
        """
        UPDATE alert_state
        SET alert_notified = CASE
            WHEN status = 'alert' AND fail_count >= COALESCE(
                (SELECT trigger_after_failures FROM alert_settings WHERE id = 1), 2
            ) THEN TRUE
            WHEN status = 'alert' THEN FALSE
            ELSE TRUE
        END
        """
    ))

    with op.batch_alter_table("alert_state") as batch_op:
        batch_op.drop_column("fail_count")

    with op.batch_alter_table("alert_settings") as batch_op:
        batch_op.drop_constraint(
            "ck_alert_settings_trigger_after_failures_range",
            type_="check",
        )
        batch_op.drop_column("trigger_after_failures")
