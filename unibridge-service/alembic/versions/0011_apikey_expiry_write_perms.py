"""Add expiry and per-key write permission columns to api_key_access.

``expires_at`` (NULL = never expires) backs the 30-day lifetime of
self-issued keys; admin-created keys stay NULL. ``allow_insert`` /
``allow_update`` / ``allow_delete`` and ``allowed_tables`` (JSON array,
NULL = all tables) give API keys the same write/table granularity that
role-based ``permissions`` rows already have.

Revision ID: 0011_apikey_expiry_write_perms
Revises: 0010_admin_audit_log
Create Date: 2026-06-10
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0011_apikey_expiry_write_perms"
down_revision = "0010_admin_audit_log"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "api_key_access",
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "api_key_access",
        sa.Column(
            "allow_insert", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
    )
    op.add_column(
        "api_key_access",
        sa.Column(
            "allow_update", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
    )
    op.add_column(
        "api_key_access",
        sa.Column(
            "allow_delete", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
    )
    op.add_column(
        "api_key_access",
        sa.Column("allowed_tables", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("api_key_access", "allowed_tables")
    op.drop_column("api_key_access", "allow_delete")
    op.drop_column("api_key_access", "allow_update")
    op.drop_column("api_key_access", "allow_insert")
    op.drop_column("api_key_access", "expires_at")
