"""Публичные гостевые миры без обязательной привязки к аккаунту."""

import sqlalchemy as sa
from alembic import op

revision = "003_guest_worlds"
down_revision = "002_world_progress"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("world_saves", sa.Column("player_id", sa.Uuid(), nullable=True))
    # Старые острова и их содержимое сохраняются, прежний UUID остаётся адресом.
    op.execute("UPDATE world_saves SET player_id = user_id")
    op.alter_column("world_saves", "player_id", nullable=False)
    op.drop_constraint("world_saves_pkey", "world_saves", type_="primary")
    op.alter_column("world_saves", "user_id", nullable=True)
    op.create_primary_key("world_saves_pkey", "world_saves", ["player_id"])
    op.create_unique_constraint("uq_world_saves_user_id", "world_saves", ["user_id"])


def downgrade() -> None:
    if op.get_bind().scalar(
        sa.text("SELECT count(*) FROM world_saves WHERE user_id IS NULL")
    ):
        raise RuntimeError("Нельзя откатить схему, пока есть гостевые сохранения")
    op.drop_constraint("uq_world_saves_user_id", "world_saves", type_="unique")
    op.drop_constraint("world_saves_pkey", "world_saves", type_="primary")
    op.alter_column("world_saves", "user_id", nullable=False)
    op.create_primary_key("world_saves_pkey", "world_saves", ["user_id"])
    op.drop_column("world_saves", "player_id")
