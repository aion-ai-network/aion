"""Exclusive upload receipt and lookup indexes."""
from alembic import op

revision = "0007_uploads"
down_revision = "0006_operations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    ALTER TABLE app.attachments ADD COLUMN receiving_at timestamptz;
    CREATE INDEX message_history ON app.messages(user_id,sequence DESC);
    CREATE INDEX usage_days ON app.usage_records(utc_day,user_id,local_day);
    CREATE INDEX notification_history ON app.web_notifications(user_id,created_at DESC);
    """)


def downgrade() -> None:
    op.execute("""
    DROP INDEX app.notification_history,app.usage_days,app.message_history;
    ALTER TABLE app.attachments DROP COLUMN receiving_at;
    """)
