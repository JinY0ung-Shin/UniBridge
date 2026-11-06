"""Network map: one record per firewall opening, and measured probes.

``firewall_openings.src_key`` ("host:<id>" or "cidr:<cidr>") folds the two
source columns into one non-null value so a unique constraint can refuse a
second record of the same opening (NULLs never collide). Existing rows are
backfilled; of duplicates a race may have left, one stays: an open one over a
closed one, then a verified one, then the newest.

``network_probes`` keeps the latest measured connection attempt per source
host, destination host, port and protocol.

Revision ID: 0032_network_probes
Revises: 0031_network_map
Create Date: 2026-11-06
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0032_network_probes"
down_revision = "0031_network_map"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("firewall_openings") as batch:
        batch.add_column(sa.Column("src_key", sa.String(length=80), nullable=True))
    op.execute(
        "UPDATE firewall_openings SET src_key = CASE "
        "WHEN src_host_id IS NOT NULL THEN 'host:' || CAST(src_host_id AS VARCHAR(20)) "
        "ELSE 'cidr:' || src_cidr END"
    )
    op.execute(
        "DELETE FROM firewall_openings WHERE id IN ("
        "SELECT id FROM (SELECT id, ROW_NUMBER() OVER ("
        "PARTITION BY src_key, dst_host_id, protocol, port_from, port_to "
        "ORDER BY CASE WHEN status = 'open' THEN 0 ELSE 1 END, "
        "CASE WHEN verified_at IS NOT NULL THEN 0 ELSE 1 END, id DESC"
        ") AS rank FROM firewall_openings) ranked WHERE rank > 1)"
    )
    with op.batch_alter_table("firewall_openings") as batch:
        batch.alter_column("src_key", existing_type=sa.String(length=80), nullable=False)
        batch.create_unique_constraint(
            "uq_firewall_openings_identity",
            ["src_key", "dst_host_id", "protocol", "port_from", "port_to"],
        )

    op.create_table(
        "network_probes",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("src_host_id", sa.Integer(), nullable=False),
        sa.Column("dst_host_id", sa.Integer(), nullable=False),
        sa.Column("port", sa.Integer(), nullable=False),
        sa.Column("protocol", sa.String(length=8), server_default="tcp", nullable=False),
        sa.Column("result", sa.String(length=16), nullable=False),
        sa.Column("latency_ms", sa.Float(), nullable=True),
        sa.Column("error", sa.String(length=255), server_default="", nullable=False),
        sa.Column("reported_by", sa.String(length=255), nullable=True),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("port >= 1 AND port <= 65535", name="ck_network_probes_port"),
        sa.ForeignKeyConstraint(["src_host_id"], ["network_hosts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["dst_host_id"], ["network_hosts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "src_host_id", "dst_host_id", "port", "protocol", name="uq_network_probes_path"
        ),
    )
    op.create_index("ix_network_probes_src_host_id", "network_probes", ["src_host_id"])
    op.create_index("ix_network_probes_dst_host_id", "network_probes", ["dst_host_id"])


def downgrade() -> None:
    op.drop_index("ix_network_probes_dst_host_id", table_name="network_probes")
    op.drop_index("ix_network_probes_src_host_id", table_name="network_probes")
    op.drop_table("network_probes")
    with op.batch_alter_table("firewall_openings") as batch:
        batch.drop_constraint("uq_firewall_openings_identity", type_="unique")
        batch.drop_column("src_key")
