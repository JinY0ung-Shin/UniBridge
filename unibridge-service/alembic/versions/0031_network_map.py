"""Add the network map: regions, hosts, firewall openings and port usage.

Hosts in one region reach each other on every port; a cross-region connection
needs a firewall opening requested through the company system. ``firewall_openings``
records those openings (one direction each), ``port_usages`` what listens on a
host port. Rows written through an API key carry ``source='agent'`` and stay
unverified until a person confirms them.

Revision ID: 0031_network_map
Revises: 0030_audit_log_template_path
Create Date: 2026-11-05
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0031_network_map"
down_revision = "0030_audit_log_template_path"
branch_labels = None
depends_on = None


def _audit_columns() -> list[sa.Column]:
    return [
        sa.Column("source", sa.String(length=16), server_default="manual", nullable=False),
        sa.Column("created_by", sa.String(length=255), nullable=True),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("verified_by", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    ]


def upgrade() -> None:
    op.create_table(
        "network_regions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("description", sa.String(length=255), server_default="", nullable=False),
        sa.Column("cidrs", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_network_regions_name", "network_regions", ["name"], unique=True)

    op.create_table(
        "network_hosts",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("ip", sa.String(length=64), nullable=False),
        sa.Column("region_id", sa.Integer(), nullable=True),
        sa.Column("description", sa.String(length=255), server_default="", nullable=False),
        sa.Column("owner", sa.String(length=255), server_default="", nullable=False),
        sa.Column("monitored_host_id", sa.Integer(), nullable=True),
        *_audit_columns(),
        sa.ForeignKeyConstraint(["region_id"], ["network_regions.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["monitored_host_id"], ["monitored_hosts.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("monitored_host_id"),
    )
    op.create_index("ix_network_hosts_name", "network_hosts", ["name"], unique=True)
    op.create_index("ix_network_hosts_ip", "network_hosts", ["ip"], unique=True)
    op.create_index("ix_network_hosts_region_id", "network_hosts", ["region_id"])

    op.create_table(
        "firewall_openings",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("src_host_id", sa.Integer(), nullable=True),
        sa.Column("src_cidr", sa.String(length=64), nullable=True),
        sa.Column("dst_host_id", sa.Integer(), nullable=False),
        sa.Column("port_from", sa.Integer(), nullable=False),
        sa.Column("port_to", sa.Integer(), nullable=False),
        sa.Column("protocol", sa.String(length=8), server_default="tcp", nullable=False),
        sa.Column("status", sa.String(length=16), server_default="open", nullable=False),
        sa.Column("ticket_no", sa.String(length=100), server_default="", nullable=False),
        sa.Column("requester", sa.String(length=100), server_default="", nullable=False),
        sa.Column("purpose", sa.String(length=500), server_default="", nullable=False),
        *_audit_columns(),
        sa.CheckConstraint(
            "(src_host_id IS NULL) <> (src_cidr IS NULL)", name="ck_firewall_openings_one_source"
        ),
        sa.CheckConstraint(
            "port_from >= 1 AND port_to <= 65535 AND port_from <= port_to",
            name="ck_firewall_openings_ports",
        ),
        sa.ForeignKeyConstraint(["src_host_id"], ["network_hosts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["dst_host_id"], ["network_hosts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_firewall_openings_src_host_id", "firewall_openings", ["src_host_id"])
    op.create_index("ix_firewall_openings_dst_host_id", "firewall_openings", ["dst_host_id"])

    op.create_table(
        "port_usages",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("host_id", sa.Integer(), nullable=False),
        sa.Column("port", sa.Integer(), nullable=False),
        sa.Column("protocol", sa.String(length=8), server_default="tcp", nullable=False),
        sa.Column("service_name", sa.String(length=100), nullable=False),
        sa.Column("description", sa.String(length=255), server_default="", nullable=False),
        sa.Column("owner", sa.String(length=255), server_default="", nullable=False),
        *_audit_columns(),
        sa.CheckConstraint("port >= 1 AND port <= 65535", name="ck_port_usages_port"),
        sa.ForeignKeyConstraint(["host_id"], ["network_hosts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "host_id", "port", "protocol", "service_name", name="uq_port_usages_host_port_service"
        ),
    )
    op.create_index("ix_port_usages_host_id", "port_usages", ["host_id"])


def downgrade() -> None:
    op.drop_index("ix_port_usages_host_id", table_name="port_usages")
    op.drop_table("port_usages")
    op.drop_index("ix_firewall_openings_dst_host_id", table_name="firewall_openings")
    op.drop_index("ix_firewall_openings_src_host_id", table_name="firewall_openings")
    op.drop_table("firewall_openings")
    op.drop_index("ix_network_hosts_region_id", table_name="network_hosts")
    op.drop_index("ix_network_hosts_ip", table_name="network_hosts")
    op.drop_index("ix_network_hosts_name", table_name="network_hosts")
    op.drop_table("network_hosts")
    op.drop_index("ix_network_regions_name", table_name="network_regions")
    op.drop_table("network_regions")
