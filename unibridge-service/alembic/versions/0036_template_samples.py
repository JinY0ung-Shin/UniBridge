"""Query template samples and their periodic check.

``query_templates.sample_params`` holds the params a template is run with to
check that it still works (``{}`` for a template without params, NULL for no
sample); ``monitor_enabled`` keeps a sample without checking it.
``alert_settings.template_check_interval_minutes`` is how often the checker
runs them (a day by default, 0 = off). ``alert_history.target`` grows to 200 characters: a
template alert's target is the template path, which may be that long.

Revision ID: 0036_template_samples
Revises: 0035_mcp_artifacts
Create Date: 2026-11-13
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0036_template_samples"
down_revision = "0035_mcp_artifacts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("query_templates") as batch:
        batch.add_column(sa.Column("sample_params", sa.Text(), nullable=True))
        batch.add_column(
            sa.Column("monitor_enabled", sa.Boolean(), nullable=False, server_default=sa.true())
        )
    with op.batch_alter_table("alert_settings") as batch:
        batch.add_column(
            sa.Column(
                "template_check_interval_minutes",
                sa.Integer(),
                nullable=False,
                server_default="1440",
            )
        )
    with op.batch_alter_table("alert_history") as batch:
        batch.alter_column(
            "target",
            existing_type=sa.String(length=100),
            type_=sa.String(length=200),
            existing_nullable=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("alert_history") as batch:
        batch.alter_column(
            "target",
            existing_type=sa.String(length=200),
            type_=sa.String(length=100),
            existing_nullable=False,
        )
    with op.batch_alter_table("alert_settings") as batch:
        batch.drop_column("template_check_interval_minutes")
    with op.batch_alter_table("query_templates") as batch:
        batch.drop_column("monitor_enabled")
        batch.drop_column("sample_params")
