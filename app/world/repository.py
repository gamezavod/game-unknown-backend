from uuid import UUID

from sqlalchemy import BigInteger, cast, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.models import WorldSave


class WorldRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def get(self, player_id: UUID, *, lock: bool = False) -> WorldSave | None:
        query = select(WorldSave).where(WorldSave.player_id == player_id)
        if lock:
            query = query.with_for_update()
        return self.db.scalar(query)

    def now_ms(self) -> int:
        value = self.db.scalar(
            select(
                cast(
                    func.floor(func.extract("epoch", func.clock_timestamp()) * 1000),
                    BigInteger,
                )
            )
        )
        assert value is not None
        return int(value)

    def add_if_missing(
        self, player_id: UUID, data: dict[str, object], stamp: int
    ) -> None:
        self.db.execute(
            insert(WorldSave)
            .values(player_id=player_id, data=data, server_saved_at=stamp)
            .on_conflict_do_nothing(index_elements=[WorldSave.player_id])
        )

    def commit(self) -> None:
        self.db.commit()

    def rollback(self) -> None:
        self.db.rollback()
