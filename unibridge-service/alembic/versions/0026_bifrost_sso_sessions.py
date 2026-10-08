"""Add bifrost_sso_sessions: Bifrost sessions UniBridge handed out, to log them out.

Bifrost OSS keeps a session for 30 days with no setting to shorten it. Each row
is a session the Bifrost sign-in handoff (app/routers/bifrost_sso.py) obtained;
the token is encrypted with ENCRYPTION_KEY, and the row is deleted once the
session has been logged out at Bifrost after ``expires_at``.

Revision ID: 0026_bifrost_sso_sessions
Revises: 0025_alert_observations
Create Date: 2026-10-08
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0026_bifrost_sso_sessions"
down_revision = "0025_alert_observations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "bifrost_sso_sessions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("actor", sa.String(length=255), nullable=False),
        sa.Column("token_encrypted", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_bifrost_sso_sessions_expires_at", "bifrost_sso_sessions", ["expires_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_bifrost_sso_sessions_expires_at", table_name="bifrost_sso_sessions")
    op.drop_table("bifrost_sso_sessions")
