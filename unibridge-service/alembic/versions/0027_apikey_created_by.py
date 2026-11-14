"""Add api_key_access.created_by: the username that issued each key.

The API key page lists the caller's own keys by default (``scope=mine``), which
needs to know who issued an admin key — ``owner`` only covers self-service keys
(it holds the owner's Keycloak sub). Existing admin keys got their issuer from
the admin audit trail at boot rather than here: in a blue-green deploy the old
color kept creating keys without one while this migration ran on the new color,
so a one-off backfill would have missed them. That boot backfill was removed
once every color ran this migration.

Revision ID: 0027_apikey_created_by
Revises: 0026_bifrost_sso_sessions
Create Date: 2026-10-10
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0027_apikey_created_by"
down_revision = "0026_bifrost_sso_sessions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "api_key_access",
        sa.Column("created_by", sa.String(length=255), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("api_key_access", "created_by")
