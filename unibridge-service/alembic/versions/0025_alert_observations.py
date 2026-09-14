"""Persist latest alert observations and incident opening evidence."""
from alembic import op
import sqlalchemy as sa

revision = "0025_alert_observations"
down_revision = "0024_resolve_after_successes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("alert_state") as batch:
        batch.add_column(sa.Column("details", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("alert_state") as batch:
        batch.drop_column("details")
