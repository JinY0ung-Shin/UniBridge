"""Persist S3 private endpoint opt-in.

Revision ID: 0003_s3_private_endpoint_opt_in
Revises: 0002_permission_role_fk
Create Date: 2026-04-25
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0003_s3_private_endpoint_opt_in"
down_revision = "0002_permission_role_fk"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "s3_connections",
        sa.Column(
            "allow_private_endpoints",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )


def downgrade() -> None:
    op.drop_column("s3_connections", "allow_private_endpoints")
