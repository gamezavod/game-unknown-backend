from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.match3.config import new_profile
from app.models import Match3ProfileRow, Match3ReceiptRow, Match3RunRow


class Match3Repository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def profile(self, player: UUID) -> Match3ProfileRow:
        self.db.execute(
            insert(Match3ProfileRow)
            .values(player_id=player, data=new_profile().model_dump(mode="json"))
            .on_conflict_do_nothing()
        )
        return self.db.scalars(
            select(Match3ProfileRow).where(Match3ProfileRow.player_id == player)
        ).one()

    def active(self, player: UUID) -> Match3RunRow | None:
        return self.db.scalar(
            select(Match3RunRow).where(
                Match3RunRow.player_id == player, Match3RunRow.status == "active"
            )
        )

    def run(self, player: UUID, run_id: UUID) -> Match3RunRow | None:
        return self.db.scalar(
            select(Match3RunRow).where(
                Match3RunRow.player_id == player, Match3RunRow.id == run_id
            )
        )

    def receipt(self, player: UUID, key: str) -> Match3ReceiptRow | None:
        return self.db.get(Match3ReceiptRow, (player, key))
