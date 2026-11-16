from uuid import UUID, uuid4

from pydantic import ValidationError

from app.config import Settings
from app.models import WorldSave
from app.world.progress import advance_growth, apply_action, new_progress
from app.world.repository import WorldRepository
from app.world.schemas import ActionRequest, OpenWorld, SaveRequest, StoredSave
from app.world.state import (
    GameProgress,
    SaveDataError,
    SaveGame,
    decode_save,
    encode_save,
)


class WorldError(Exception):
    def __init__(
        self, status: int, detail: str, current: StoredSave | None = None
    ) -> None:
        self.status, self.detail, self.current = status, detail, current


def wire(save: SaveGame) -> StoredSave:
    assert save.serverSavedAt is not None
    return StoredSave(data=encode_save(save), serverSavedAt=save.serverSavedAt)


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
        self.repo.commit()
        return wire(saved)

    def locked(self, user_id: UUID) -> tuple[WorldSave, SaveGame]:
        row = self.repo.get(user_id, lock=True)
        if row is None:
            self.repo.rollback()
            raise WorldError(404, "Сначала создайте мир")
        return row, self.read(row)

    def check_stamp(self, save: SaveGame, expected: int | float | None) -> None:
        if expected != save.serverSavedAt:
            self.repo.rollback()
            raise WorldError(
                409, "На сервере уже есть более свежее сохранение", wire(save)
            )

    def open(self, user_id: UUID, body: OpenWorld) -> StoredSave:
        row = self.repo.get(user_id)
        if row is not None:
            return self.load(user_id)
        if body.initialSave is not None:
            progress = self.decode(body.initialSave).progress
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
        self.repo.add_if_missing(user_id, save.model_dump(), now)
        # Один остров на аккаунт, включая одновременное создание с двух устройств.
        return self.load(user_id)

    def load(self, user_id: UUID) -> StoredSave:
        row, save = self.locked(user_id)
        grown = advance_growth(save.progress, self.repo.now_ms())
        if grown != save.progress:
            return self.record(row, grown, save)
        self.repo.commit()
        return wire(save)

    def action(self, user_id: UUID, body: ActionRequest) -> StoredSave:
        row, save = self.locked(user_id)
        self.check_stamp(save, body.expectedServerSavedAt)
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
            return wire(save)
        return self.record(row, progress, save)

    def replace(self, user_id: UUID, body: SaveRequest) -> StoredSave:
        incoming = self.decode(body.data)
        row, save = self.locked(user_id)
        self.check_stamp(save, body.expectedServerSavedAt)
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
        if incoming.progress == save.progress:
            self.repo.commit()
            return wire(save)
        return self.record(row, incoming.progress, save)
