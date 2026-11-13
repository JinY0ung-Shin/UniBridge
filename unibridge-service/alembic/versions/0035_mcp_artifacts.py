"""Add mcp_artifacts: binary files staged for an API key on the MCP surface.

``download_artifact`` copies a source file (S3 object, NAS file, a gateway
API's binary answer) into a row and answers with its id, size, checksum and
MIME type; the MCP host then fetches the bytes from /api/mcp/artifacts/<id>.
A host stages a workspace file the same way for ``upload_staged_artifacts``
(app/services/mcp_artifacts.py). A row belongs to the key that staged it
(``consumer``) and is deleted once ``expires_at`` has passed. In the meta
database so both blue/green colors serve the same artifacts; the backups skip
this table's rows.

Revision ID: 0035_mcp_artifacts
Revises: 0034_upload_relay_files
Create Date: 2026-11-13
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0035_mcp_artifacts"
down_revision = "0034_upload_relay_files"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "mcp_artifacts",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("consumer", sa.String(length=100), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("filename", sa.String(length=255), nullable=False),
        sa.Column("content_type", sa.String(length=255), nullable=False),
        sa.Column("size", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("content", sa.LargeBinary(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_mcp_artifacts_consumer", "mcp_artifacts", ["consumer"])
    op.create_index("ix_mcp_artifacts_expires_at", "mcp_artifacts", ["expires_at"])


def downgrade() -> None:
    op.drop_index("ix_mcp_artifacts_expires_at", table_name="mcp_artifacts")
    op.drop_index("ix_mcp_artifacts_consumer", table_name="mcp_artifacts")
    op.drop_table("mcp_artifacts")
