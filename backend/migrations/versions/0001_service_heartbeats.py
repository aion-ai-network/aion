"""Add service heartbeats for the runtime scaffold."""
from alembic import op

revision = "0001_service_heartbeats"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE app.service_heartbeats (
          component varchar(32) PRIMARY KEY,
          instance_id varchar(128) NOT NULL,
          observed_at timestamptz NOT NULL
        )
    """)


def downgrade() -> None:
    op.execute("DROP TABLE app.service_heartbeats")
