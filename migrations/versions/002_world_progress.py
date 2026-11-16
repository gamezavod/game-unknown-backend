"""Хранение мира и прогресса, привязанных к существующему аккаунту."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "002_world_progress"
down_revision = "001_auth"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "world_saves",
        sa.Column(
            "user_id",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("data", postgresql.JSONB(), nullable=False),
        sa.Column("server_saved_at", sa.BigInteger(), nullable=False),
        sa.CheckConstraint("server_saved_at >= 0", name="ck_world_saved_at"),
    )


def downgrade() -> None:
    op.drop_table("world_saves")
