"""Сохранение экономики прототипа, активных смен и ответов на повторы запросов."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "004_match3"
down_revision = "003_guest_worlds"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "match3_profiles",
        sa.Column(
            "player_id",
            sa.Uuid(),
            sa.ForeignKey("world_saves.player_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("data", postgresql.JSONB(), nullable=False),
    )
    op.create_table(
        "match3_runs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "player_id",
            sa.Uuid(),
            sa.ForeignKey("world_saves.player_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("data", postgresql.JSONB(), nullable=False),
        sa.CheckConstraint(
            "status IN ('active', 'finished')", name="ck_match3_run_status"
        ),
    )
    op.create_index("ix_match3_runs_player_id", "match3_runs", ["player_id"])
    op.create_index(
        "uq_match3_active_player",
        "match3_runs",
        ["player_id"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )
    op.create_table(
        "match3_receipts",
        sa.Column(
            "player_id",
            sa.Uuid(),
            sa.ForeignKey("world_saves.player_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("key", sa.String(128), primary_key=True),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("response", postgresql.JSONB(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("match3_receipts")
    op.drop_table("match3_runs")
    op.drop_table("match3_profiles")
