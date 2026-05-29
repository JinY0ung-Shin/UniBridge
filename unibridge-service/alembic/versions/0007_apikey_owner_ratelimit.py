"""Add owner and rate_limit_per_minute to api_key_access.

Revision ID: 0007_apikey_owner_ratelimit
Revises: 0006_query_templates
Create Date: 2026-05-29
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0007_apikey_owner_ratelimit"
down_revision = "0006_query_templates"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("api_key_access", sa.Column("owner", sa.String(length=255), nullable=True))
    op.add_column("api_key_access", sa.Column("rate_limit_per_minute", sa.Integer(), nullable=True))
    op.create_index("ix_api_key_access_owner", "api_key_access", ["owner"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_api_key_access_owner", table_name="api_key_access")
    op.drop_column("api_key_access", "rate_limit_per_minute")
    op.drop_column("api_key_access", "owner")
