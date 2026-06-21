"""Add route_error_min_requests floor to alert_settings.

Suppresses 5xx error-rate alerts on low-traffic routes where a single error
inflates the percentage past the threshold.

Revision ID: 0015_route_error_min_requests
Revises: 0014_server_monitoring
Create Date: 2026-06-21
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0015_route_error_min_requests"
down_revision = "0014_server_monitoring"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("alert_settings") as batch_op:
        batch_op.add_column(
            sa.Column(
                "route_error_min_requests",
                sa.Integer(),
                nullable=False,
                server_default="20",
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("alert_settings") as batch_op:
        batch_op.drop_column("route_error_min_requests")
