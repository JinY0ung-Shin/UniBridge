"""Network map: hosts we run vs. someone else's servers (network_hosts.kind).

``managed`` (the default) is a server we run; ``external`` is a destination we
only record openings to, kept out of region reach, the port finder and the
host counts. The ``dst-<ip>`` placeholders people created only because an
opening needed a destination row become ``external`` — when they are in no
region, linked to no monitored server and not the platform host, so nothing
they answer changes. The rest stays ``managed`` and is switched by hand.

Revision ID: 0033_network_host_kind
Revises: 0032_network_probes
Create Date: 2026-11-06
"""
from __future__ import annotations

import json

from alembic import op
import sqlalchemy as sa


revision = "0033_network_host_kind"
down_revision = "0032_network_probes"
branch_labels = None
depends_on = None


def _platform_host_id(bind: sa.engine.Connection) -> int | None:
    value = bind.execute(
        sa.text("SELECT value FROM system_config WHERE key = 'network_settings'")
    ).scalar()
    try:
        host_id = json.loads(value).get("platform_host_id") if value else None
    except (TypeError, ValueError, AttributeError):
        return None
    return host_id if isinstance(host_id, int) else None


def upgrade() -> None:
    with op.batch_alter_table("network_hosts") as batch:
        batch.add_column(
            sa.Column("kind", sa.String(length=16), nullable=False, server_default="managed")
        )
    bind = op.get_bind()
    platform = _platform_host_id(bind)
    bind.execute(
        sa.text(
            "UPDATE network_hosts SET kind = 'external' "
            "WHERE LOWER(name) LIKE 'dst-%' AND region_id IS NULL AND monitored_host_id IS NULL "
            "AND id <> :platform"
        ),
        {"platform": platform if platform is not None else -1},
    )


def downgrade() -> None:
    with op.batch_alter_table("network_hosts") as batch:
        batch.drop_column("kind")
