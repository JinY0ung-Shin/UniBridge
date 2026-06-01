"""Add nas_connections table for the read-only NAS provider.

Revision ID: 0008_nas_connections
Revises: 0007_apikey_owner_ratelimit
Create Date: 2026-06-01
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0008_nas_connections"
down_revision = "0007_apikey_owner_ratelimit"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "nas_connections",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("alias", sa.String(), nullable=False),
        sa.Column("base_path", sa.String(), nullable=False),
        sa.Column("read_only", sa.Boolean(), nullable=True),
        sa.Column("max_download_bytes", sa.Integer(), nullable=True),
        sa.Column("show_hidden", sa.Boolean(), nullable=True),
        sa.Column("follow_symlinks", sa.Boolean(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_nas_connections_alias", "nas_connections", ["alias"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_nas_connections_alias", table_name="nas_connections")
    op.drop_table("nas_connections")
