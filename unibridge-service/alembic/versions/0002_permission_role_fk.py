"""Add cascading FK from permissions.role to roles.name.

Revision ID: 0002_permission_role_fk
Revises: 0001_initial
Create Date: 2026-04-25
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0002_permission_role_fk"
down_revision = "0001_initial"
branch_labels = None
depends_on = None

FK_NAME = "fk_permissions_role_roles_name"


def upgrade() -> None:
    op.execute(
        "DELETE FROM permissions "
        "WHERE role NOT IN (SELECT name FROM roles)"
    )

    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        with op.batch_alter_table("permissions", recreate="always") as batch_op:
            batch_op.alter_column(
                "role",
                existing_type=sa.String(),
                type_=sa.String(length=100),
                existing_nullable=False,
            )
            batch_op.create_foreign_key(
                FK_NAME,
                "roles",
                ["role"],
                ["name"],
                ondelete="CASCADE",
                onupdate="CASCADE",
            )
    else:
        op.alter_column(
            "permissions",
            "role",
            existing_type=sa.String(),
            type_=sa.String(length=100),
            existing_nullable=False,
        )
        op.create_foreign_key(
            FK_NAME,
            "permissions",
            "roles",
            ["role"],
            ["name"],
            ondelete="CASCADE",
            onupdate="CASCADE",
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        with op.batch_alter_table("permissions", recreate="always") as batch_op:
            batch_op.drop_constraint(FK_NAME, type_="foreignkey")
    else:
        op.drop_constraint(FK_NAME, "permissions", type_="foreignkey")
