"""Add service heartbeats for the runtime scaffold."""
from alembic import op
import sqlalchemy as sa

revision = "0001_service_heartbeats"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "service_heartbeats",
        sa.Column("component", sa.String(32), primary_key=True),
        sa.Column("instance_id", sa.String(128), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        schema="app",
    )


def downgrade() -> None:
    op.drop_table("service_heartbeats", schema="app")
