"""Схема совпадает с DTO существующего фронтенда, включая camelCase в JSON."""

from __future__ import annotations

import json
from typing import Annotated, Any, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    model_serializer,
    model_validator,
)

SafeInt = Annotated[int, Field(ge=-(2**53 - 1), le=2**53 - 1)]
Natural = Annotated[int, Field(ge=0, le=2**53 - 1)]
Timestamp = Annotated[int | float, Field(ge=0)]
Terrain = Literal["grass", "forest", "rock", "coast", "water", "river", "fog"]
Building = Literal["townHall", "home", "lumbermill", "farm", "warehouse"]


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class Position(Model):
    q: SafeInt
    r: SafeInt

    @property
    def key(self) -> tuple[int, int]:
        return self.q, self.r


class Cell(Position):
    type: Terrain
    height: Annotated[float, Field(ge=0)]
    state: Literal["visible", "unexplored"]
    rotation: float | None = None
    variant: Annotated[int, Field(ge=0, le=2)] | None = None
    building: Building | None = None
    buildingLevel: Annotated[int, Field(ge=1)] | None = None

    @model_validator(mode="after")
    def validate_building(self) -> Self:
        if self.buildingLevel is not None and self.building is None:
            raise ValueError("Уровень задан без здания")
        if self.building and (self.state != "visible" or self.type != "grass"):
            raise ValueError("Здание находится на недопустимой клетке")
        if self.type == "fog" and self.state != "unexplored":
            raise ValueError("Туман не может быть открытой клеткой")
        return self

    @model_serializer(mode="wrap")
    def serialize_cell(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        # JS DTO использует отсутствие необязательного поля, а не null.
        return {key: value for key, value in handler(self).items() if value is not None}


class World(Model):
    seed: Annotated[str, Field(min_length=1)]
    generatorVersion: Annotated[int, Field(ge=1)]
    cells: Annotated[list[Cell], Field(min_length=1)]

    @model_validator(mode="after")
    def validate_cells(self) -> Self:
        if len({cell.key for cell in self.cells}) != len(self.cells):
            raise ValueError("Повтор координат")
        if any(cell.state != "visible" or cell.type == "fog" for cell in self.cells):
            raise ValueError("В сохранении должны быть только открытые клетки")
        return self


class Sapling(Position):
    plantedAt: Timestamp
    growthDurationMs: Annotated[int | float, Field(gt=0)]


class ExcavationSite(Position):
    id: Annotated[str, Field(min_length=1)]


class ActiveExcavation(Model):
    siteId: str
    depth: Natural
    paid: bool


class CollectionItem(Model):
    id: Annotated[str, Field(min_length=1)]
    siteId: str
    foundAt: Timestamp


class GameProgress(Model):
    world: World
    saplings: list[Sapling]
    excavationSites: list[ExcavationSite]
    activeExcavation: ActiveExcavation | None
    collection: list[CollectionItem]
    completedShifts: Natural

    @model_validator(mode="after")
    def validate_references(self) -> Self:
        cells = {cell.key: cell for cell in self.world.cells}
        if len({s.key for s in self.saplings}) != len(self.saplings):
            raise ValueError("Повтор саженцев")
        for sapling in self.saplings:
            cell = cells.get(sapling.key)
            if not cell or cell.type != "grass" or cell.building:
                raise ValueError("Саженец вне свободной травы")
        sites = {site.id for site in self.excavationSites}
        if len(sites) != len(self.excavationSites) or len(
            {s.key for s in self.excavationSites}
        ) != len(self.excavationSites):
            raise ValueError("Повтор участков раскопок")
        if any(site.key not in cells for site in self.excavationSites):
            raise ValueError("Раскопка вне открытой карты")
        if self.activeExcavation and self.activeExcavation.siteId not in sites:
            raise ValueError("Неизвестный участок текущей раскопки")
        if len({item.id for item in self.collection}) != len(self.collection):
            raise ValueError("Повтор предметов коллекции")
        if any(item.siteId not in sites for item in self.collection):
            raise ValueError("Предмет ссылается на неизвестную раскопку")
        return self


class SaveGame(Model):
    schemaVersion: Literal[2] = 2
    savedAt: Timestamp
    serverSavedAt: Timestamp | None
    revision: Natural
    cloudPending: bool
    progress: GameProgress


class SaveDataError(ValueError):
    pass


def decode_save(data: str | dict[str, Any]) -> SaveGame:
    """Чистая миграция: исходные данные не перезаписываются при чтении."""
    try:
        raw = json.loads(data) if isinstance(data, str) else data.copy()
        if not isinstance(raw, dict):
            raise ValueError("Ожидается объект сохранения")
        if type(raw.get("schemaVersion")) is not int:
            raise ValueError("Неверная версия схемы сохранения")
        if raw.get("schemaVersion") == 1:
            raw = {
                "schemaVersion": 2,
                "savedAt": raw["savedAt"],
                "serverSavedAt": raw.get("serverSavedAt"),
                "revision": 0,
                "cloudPending": True,
                "progress": {
                    key: raw[key]
                    for key in (
                        "world",
                        "saplings",
                        "excavationSites",
                        "activeExcavation",
                        "collection",
                    )
                }
                | {"completedShifts": raw.get("completedShifts", 0)},
            }
        if raw.get("schemaVersion") != 2:
            raise ValueError(
                f"Неизвестная версия сохранения: {raw.get('schemaVersion')}"
            )
        return SaveGame.model_validate(raw)
    except (ValueError, TypeError, KeyError, AttributeError) as error:
        raise SaveDataError(
            f"Сохранение повреждено или несовместимо: {error}"
        ) from error


def encode_save(save: SaveGame) -> str:
    return save.model_dump_json()
