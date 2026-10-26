"""Add mcp_remote_servers: external MCP servers whose tools /api/mcp offers.

Each row is relayed through the APISIX route ``mcp-ext-<name>``; the upstream's
credentials are encrypted with ENCRYPTION_KEY, and ``tools``/``approved_tools``
hold the fetched tool listing and the entries an administrator approved.

Revision ID: 0028_mcp_remote_servers
Revises: 0027_apikey_created_by
Create Date: 2026-10-26
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0028_mcp_remote_servers"
down_revision = "0027_apikey_created_by"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "mcp_remote_servers",
        sa.Column("name", sa.String(length=16), primary_key=True),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("timeout_seconds", sa.Integer(), nullable=False),
        sa.Column("headers_encrypted", sa.Text(), nullable=True),
        sa.Column("tools", sa.Text(), nullable=False),
        sa.Column("approved_tools", sa.Text(), nullable=False),
        sa.Column("refreshed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("mcp_remote_servers")
