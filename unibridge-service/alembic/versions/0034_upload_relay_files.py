"""Add upload_relay_files: files waiting for the Noah server to collect them.

A Noah user uploads a file on UniBridge's /upload-relay page under a ticket
Noah signed (app/routers/upload_relay.py). The row holds the bytes until the
Noah server has downloaded and acknowledged the file, or until ``expires_at``;
either way the row is deleted. ``collected_at`` is Noah's first download. In
the meta database so both blue/green colors serve the same files; the backups
skip this table's rows.

Revision ID: 0034_upload_relay_files
Revises: 0033_network_host_kind
Create Date: 2026-11-12
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0034_upload_relay_files"
down_revision = "0033_network_host_kind"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "upload_relay_files",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("ticket_id", sa.String(length=64), nullable=False),
        sa.Column("noah_user", sa.String(length=200), nullable=False),
        sa.Column("noah_name", sa.String(length=200), nullable=True),
        sa.Column("uploader", sa.String(length=255), nullable=False),
        sa.Column("filename", sa.String(length=255), nullable=False),
        sa.Column("size", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("content", sa.LargeBinary(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("collected_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_upload_relay_files_ticket_id", "upload_relay_files", ["ticket_id"]
    )
    op.create_index(
        "ix_upload_relay_files_expires_at", "upload_relay_files", ["expires_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_upload_relay_files_expires_at", table_name="upload_relay_files")
    op.drop_index("ix_upload_relay_files_ticket_id", table_name="upload_relay_files")
    op.drop_table("upload_relay_files")
