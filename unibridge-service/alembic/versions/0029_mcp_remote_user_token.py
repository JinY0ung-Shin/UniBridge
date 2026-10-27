"""Add the per-call end-user token to mcp_remote_servers.

``user_token_header`` names the header the external server reads a user's token
from (NULL = none); ``user_token_format`` shapes its value around ``{token}``.
The token itself comes with each /api/mcp request and is never stored;
``user_token_refresh_encrypted`` is the administrator's token for refresh only,
kept apart from ``headers_encrypted`` so no relay route ever carries it.

Revision ID: 0029_mcp_remote_user_token
Revises: 0028_mcp_remote_servers
Create Date: 2026-10-27
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0029_mcp_remote_user_token"
down_revision = "0028_mcp_remote_servers"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("mcp_remote_servers") as batch:
        batch.add_column(sa.Column("user_token_header", sa.String(length=64), nullable=True))
        batch.add_column(sa.Column("user_token_format", sa.String(length=256), nullable=True))
        batch.add_column(sa.Column("user_token_refresh_encrypted", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("mcp_remote_servers") as batch:
        batch.drop_column("user_token_refresh_encrypted")
        batch.drop_column("user_token_format")
        batch.drop_column("user_token_header")
