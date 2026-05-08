"""Add owner-based alert routing schema.

Revision ID: 0004_alert_owner_routing
Revises: 0003_alert_state
Create Date: 2026-05-08
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0004_alert_owner_routing"
down_revision = "0003_alert_state"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("alert_channels") as batch_op:
        batch_op.add_column(sa.Column("recipient_item_template", sa.Text(), nullable=True))

    op.create_table(
        "owner_groups",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("emails", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("name"),
    )
    op.create_table(
        "resource_owners",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("resource_type", sa.String(length=20), nullable=False),
        sa.Column("resource_id", sa.String(length=200), nullable=False),
        sa.Column("owner_group_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["owner_group_id"], ["owner_groups.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("resource_type", "resource_id", name="uq_resource_owner_type_id"),
    )
    op.create_table(
        "alert_settings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("mail_channel_id", sa.Integer(), nullable=True),
        sa.Column("fallback_owner_group_id", sa.Integer(), nullable=True),
        sa.Column("route_error_threshold_pct", sa.Float(), nullable=False, server_default="10.0"),
        sa.Column("check_interval_seconds", sa.Integer(), nullable=False, server_default="60"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("id = 1", name="ck_alert_settings_singleton"),
        sa.ForeignKeyConstraint(["mail_channel_id"], ["alert_channels.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["fallback_owner_group_id"], ["owner_groups.id"], ondelete="RESTRICT"),
    )

    with op.batch_alter_table("alert_history") as batch_op:
        batch_op.add_column(sa.Column("owner_group_id", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("resource_type", sa.String(length=20), nullable=True))
        batch_op.create_foreign_key(
            "fk_alert_history_owner_group_id_owner_groups",
            "owner_groups",
            ["owner_group_id"],
            ["id"],
            ondelete="SET NULL",
        )

    op.execute(sa.text(
        "INSERT INTO alert_settings "
        "(id, route_error_threshold_pct, check_interval_seconds, updated_at) "
        "VALUES (1, 10.0, 60, CURRENT_TIMESTAMP)"
    ))


def downgrade() -> None:
    with op.batch_alter_table("alert_history") as batch_op:
        batch_op.drop_constraint(
            "fk_alert_history_owner_group_id_owner_groups",
            type_="foreignkey",
        )
        batch_op.drop_column("resource_type")
        batch_op.drop_column("owner_group_id")

    op.drop_table("alert_settings")
    op.drop_table("resource_owners")
    op.drop_table("owner_groups")

    with op.batch_alter_table("alert_channels") as batch_op:
        batch_op.drop_column("recipient_item_template")
