from uuid import UUID, uuid4

from pydantic import ValidationError
from sqlalchemy import select

from app.config import Settings
from app.models import Match3RunRow, WorldSave
from app.world.generator import GENERATOR_V2, can_generate_cell, fog_preview
from app.world.progress import advance_growth, apply_action, new_progress
from app.world.repository import WorldRepository
from app.world.schemas import ActionRequest, OpenWorld, SaveRequest, StoredSave
from app.world.state import (
    GameProgress,
    SaveDataError,
    SaveGame,
    World,
    decode_save,
    encode_save,
)


class WorldError(Exception):
    def __init__(
        self, status: int, detail: str, current: StoredSave | None = None
    ) -> None:
        self.status, self.detail, self.current = status, detail, current


def wire(save: SaveGame, player_id: UUID) -> StoredSave:
    assert save.serverSavedAt is not None
    return StoredSave(
        playerId=player_id,
        data=encode_save(save),
        serverSavedAt=save.serverSavedAt,
        fogPreview=fog_preview(save.progress.world),
    )


class WorldService:
    def __init__(self, repo: WorldRepository, settings: Settings) -> None:
        self.repo, self.settings = repo, settings

    def decode(self, value: str | dict[str, object]) -> SaveGame:
        if (
            isinstance(value, str)
            and len(value.encode()) > self.settings.max_save_bytes
        ):
            raise WorldError(413, "Снимок превышает настроенный размер запроса")
        try:
            return decode_save(value)
        except SaveDataError:
            self.repo.rollback()
            raise WorldError(
                422,
                "Сохранение повреждено или несовместимо. "
                "Данные оставлены без изменений.",
            ) from None

    def read(self, row: WorldSave) -> SaveGame:
        save = self.decode(row.data)
        return save.model_copy(
            update={"serverSavedAt": row.server_saved_at, "cloudPending": False}
        )

    def record(
        self, row: WorldSave, progress: GameProgress, previous: SaveGame
    ) -> StoredSave:
        now = self.repo.now_ms()
        assert previous.serverSavedAt is not None
        stamp = max(now, int(previous.serverSavedAt) + 1)
        saved = SaveGame(
            savedAt=now,
            serverSavedAt=stamp,
            revision=previous.revision + 1,
            cloudPending=False,
            progress=progress,
        )
        row.data = saved.model_dump()
        row.server_saved_at = stamp
        player_id = row.player_id
        self.repo.commit()
        return wire(saved, player_id)

    def locked(self, player_id: UUID) -> tuple[WorldSave, SaveGame]:
        row = self.repo.get(player_id, lock=True)
        if row is None:
            self.repo.rollback()
            raise WorldError(404, "Сначала создайте мир")
        return row, self.read(row)

    def check_stamp(
        self, save: SaveGame, expected: int | float | None, player_id: UUID
    ) -> None:
        if expected != save.serverSavedAt:
            self.repo.rollback()
            raise WorldError(
                409,
                "На сервере уже есть более свежее сохранение",
                wire(save, player_id),
            )

    def open(self, player_id: UUID, body: OpenWorld) -> StoredSave:
        row = self.repo.get(player_id)
        if row is not None:
            return self.load(player_id)
        if body.initialSave is not None:
            progress = self.decode(body.initialSave).progress
            self.validate_footprint(progress.world)
        else:
            try:
                progress = new_progress(
                    body.seed if body.seed is not None else str(uuid4())
                )
            except ValueError as error:
                raise WorldError(422, str(error)) from error
        now = self.repo.now_ms()
        save = SaveGame(
            savedAt=now,
            serverSavedAt=now,
            revision=0,
            cloudPending=False,
            progress=progress,
        )
        self.repo.add_if_missing(player_id, save.model_dump(), now)
        # Один остров на playerId, включая одновременное создание с двух устройств.
        return self.load(player_id)

    def load(self, player_id: UUID) -> StoredSave:
        row, save = self.locked(player_id)
        grown = advance_growth(save.progress, self.repo.now_ms())
        if grown != save.progress:
            return self.record(row, grown, save)
        self.repo.commit()
        return wire(save, player_id)

    def action(self, player_id: UUID, body: ActionRequest) -> StoredSave:
        row, save = self.locked(player_id)
        self.check_stamp(save, body.expectedServerSavedAt, player_id)
        try:
            progress = apply_action(
                save.progress,
                body.action.model_dump(),
                self.repo.now_ms(),
                self.settings.sapling_growth_ms,
            )
        except (ValueError, ValidationError) as error:
            self.repo.rollback()
            raise WorldError(400, str(error)) from error
        if progress == save.progress:
            self.repo.commit()
            return wire(save, player_id)
        return self.record(row, progress, save)

    def validate_footprint(self, world: World) -> None:
        if world.generatorVersion == GENERATOR_V2 and any(
            c.type == "water" or not can_generate_cell(world, c.key)
            for c in world.cells
        ):
            self.repo.rollback()
            raise WorldError(400, "Снимок содержит клетки за пределами острова")

    def replace(self, player_id: UUID, body: SaveRequest) -> StoredSave:
        incoming = self.decode(body.data)
        row, save = self.locked(player_id)
        self.check_stamp(save, body.expectedServerSavedAt, player_id)
        active_dig = self.repo.db.scalar(
            select(Match3RunRow.id).where(
                Match3RunRow.player_id == player_id,
                Match3RunRow.status == "active",
                Match3RunRow.data["rules"]["shiftId"].as_string() == "dig",
            )
        )
        if active_dig is not None and (
            incoming.progress.activeExcavation != save.progress.activeExcavation
            or {s.id: s for s in incoming.progress.excavationSites}
            != {s.id: s for s in save.progress.excavationSites}
            or {item.id: item for item in incoming.progress.collection}
            != {item.id: item for item in save.progress.collection}
        ):
            self.repo.rollback()
            raise WorldError(
                409, "Активная раскопка изменяется только через match3 API"
            )
        if (
            incoming.progress.world.seed != save.progress.world.seed
            or incoming.progress.world.generatorVersion
            != save.progress.world.generatorVersion
        ):
            self.repo.rollback()
            raise WorldError(
                400, "Снимок принадлежит другому миру или версии генератора"
            )
        if not {c.key for c in save.progress.world.cells}.issubset(
            {c.key for c in incoming.progress.world.cells}
        ):
            self.repo.rollback()
            raise WorldError(400, "Снимок теряет уже открытые клетки")
        self.validate_footprint(incoming.progress.world)
        if incoming.progress == save.progress:
            self.repo.commit()
            return wire(save, player_id)
        return self.record(row, incoming.progress, save)
