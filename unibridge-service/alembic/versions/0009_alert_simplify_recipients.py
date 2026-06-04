"""Simplify alert routing to per-resource assignees + global admins.

Replaces the owner-group / resource-owner-rule / alert-rule model with a flat
recipient model:

- ``resource_owners`` stores assignee emails (담당자) directly instead of an
  ``owner_group_id`` FK.
- ``alert_settings`` gains ``admin_emails`` (관리자 — recipients of every alert)
  and drops ``fallback_owner_group_id``.
- ``owner_groups``, ``alert_rules`` and ``alert_rule_channels`` are dropped.
- ``alert_history`` drops its now-dangling ``rule_id`` / ``owner_group_id`` FKs.

Existing data is migrated: each resource-owner mapping inherits the emails of
its former owner-group; the former fallback owner-group's emails become the
global admin list.

Revision ID: 0009_alert_simplify_recipients
Revises: 0008_nas_connections
Create Date: 2026-06-04
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0009_alert_simplify_recipients"
down_revision = "0008_nas_connections"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. resource_owners: add emails, backfill from owner_groups, drop owner_group_id.
    with op.batch_alter_table("resource_owners") as batch_op:
        batch_op.add_column(sa.Column("emails", sa.Text(), nullable=True))

    op.execute(
        "UPDATE resource_owners SET emails = ("
        " SELECT og.emails FROM owner_groups og"
        " WHERE og.id = resource_owners.owner_group_id"
        ") WHERE emails IS NULL"
    )
    op.execute("UPDATE resource_owners SET emails = '[]' WHERE emails IS NULL")

    with op.batch_alter_table("resource_owners") as batch_op:
        batch_op.alter_column("emails", existing_type=sa.Text(), nullable=False)
        batch_op.drop_column("owner_group_id")

    # 2. alert_settings: add admin_emails, backfill from fallback group, drop fallback FK.
    with op.batch_alter_table("alert_settings") as batch_op:
        batch_op.add_column(
            sa.Column("admin_emails", sa.Text(), nullable=False, server_default="[]")
        )

    op.execute(
        "UPDATE alert_settings SET admin_emails = COALESCE("
        " (SELECT og.emails FROM owner_groups og"
        "  WHERE og.id = alert_settings.fallback_owner_group_id),"
        " '[]')"
    )

    with op.batch_alter_table("alert_settings") as batch_op:
        batch_op.drop_column("fallback_owner_group_id")

    # 3. alert_history: drop columns whose FKs point at tables we are dropping.
    with op.batch_alter_table("alert_history") as batch_op:
        batch_op.drop_column("owner_group_id")
        batch_op.drop_column("rule_id")

    # 4. Drop the obsolete tables (children first for FK safety).
    op.drop_table("alert_rule_channels")
    op.drop_table("alert_rules")
    op.drop_table("owner_groups")


def downgrade() -> None:
    # Structural rollback only — original owner-group / rule data is not restored.
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
        "alert_rules",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("type", sa.String(length=30), nullable=False),
        sa.Column("target", sa.String(length=100), nullable=False),
        sa.Column("threshold", sa.Float(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_table(
        "alert_rule_channels",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("rule_id", sa.Integer(), nullable=False),
        sa.Column("channel_id", sa.Integer(), nullable=False),
        sa.Column("recipients", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["rule_id"], ["alert_rules.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["channel_id"], ["alert_channels.id"], ondelete="CASCADE"),
    )

    with op.batch_alter_table("alert_history") as batch_op:
        batch_op.add_column(sa.Column("rule_id", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("owner_group_id", sa.Integer(), nullable=True))

    with op.batch_alter_table("alert_settings") as batch_op:
        batch_op.add_column(sa.Column("fallback_owner_group_id", sa.Integer(), nullable=True))
    op.execute("UPDATE alert_settings SET fallback_owner_group_id = NULL")
    with op.batch_alter_table("alert_settings") as batch_op:
        batch_op.drop_column("admin_emails")

    with op.batch_alter_table("resource_owners") as batch_op:
        batch_op.add_column(sa.Column("owner_group_id", sa.Integer(), nullable=True))
    # No owner-groups exist to map back to; clear rows that can't satisfy the
    # restored NOT NULL FK rather than fabricate group ids.
    op.execute("DELETE FROM resource_owners")
    with op.batch_alter_table("resource_owners") as batch_op:
        batch_op.alter_column("owner_group_id", existing_type=sa.Integer(), nullable=False)
        batch_op.drop_column("emails")
