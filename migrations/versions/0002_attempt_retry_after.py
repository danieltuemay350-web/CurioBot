"""Persist Telegram-provided retry-after delays."""

from alembic import op
import sqlalchemy as sa

revision = "0002_attempt_retry_after"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("publication_attempts", sa.Column("retry_after_seconds", sa.Float(), nullable=True))


def downgrade() -> None:
    op.drop_column("publication_attempts", "retry_after_seconds")
