from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import BeforeValidator, ConfigDict, Field, model_validator

from app.world.state import CollectionItem, Model, Natural, Position, Timestamp

ShiftId = Literal["sawmill", "farm", "quarry", "mine", "dig"]
ResourceId = Literal["wood", "food", "stone", "iron"]


def canonical_chip(value: object) -> object:
    """Старые партии и receipts читаются без второго визуального типа камня."""
    return "rock" if value == "digRock" else value


ChipId = Annotated[
    Literal[
        "log",
        "tree",
        "leaf",
        "acorn",
        "mushroom",
        "wheat",
        "carrot",
        "apple",
        "sunflower",
        "pumpkin",
        "rock",
        "sand",
        "brick",
        "crystal",
        "clay",
        "ore",
        "gem",
        "lantern",
        "pickaxe",
        "gear",
        "sprout",
        "autumn",
    ],
    BeforeValidator(canonical_chip),
]
Board = list[list[ChipId]]


class CellPosition(Model):
    row: Annotated[int, Field(ge=0, le=7)]
    column: Annotated[int, Field(ge=0, le=7)]

    @property
    def key(self) -> tuple[int, int]:
        return self.row, self.column


class Resources(Model):
    wood: Natural = 0
    food: Natural = 0
    stone: Natural = 0
    iron: Natural = 0

    def amount(self, resource: ResourceId) -> int:
        return int(getattr(self, resource))

    def put(self, resource: ResourceId, amount: int) -> None:
        setattr(self, resource, amount)


class Rules(Model):
    shiftId: ShiftId
    title: str
    resourceId: ResourceId | None
    mainChip: ChipId
    chips: Annotated[list[ChipId], Field(min_length=3)]
    target: Annotated[int, Field(gt=0)]
    bonus: Natural
    moves: Annotated[int, Field(gt=0)]
    charges: Natural
    requiredWorkers: Natural

    @model_validator(mode="after")
    def unique_dig_chips(self) -> Self:
        if self.shiftId == "dig":
            # В старых snapshots были и rock, и digRock. После приведения
            # оставляем один элемент, чтобы камень не выпадал с двойным весом.
            self.chips = list(dict.fromkeys(self.chips))
        return self


class Reason(Model):
    code: str
    message: str


class Goal(Model):
    type: Literal["collect", "amphora"]
    chipId: ChipId | None
    target: int
    progress: int
    bonus: int
    targetDepth: int | None = None


class Stock(Model):
    amount: Natural
    limit: Natural


class Warehouse(Model):
    wood: Stock
    food: Stock
    stone: Stock
    iron: Stock


class DigResult(Model):
    siteId: str
    depth: int
    savedDepth: int
    paid: bool
    finding: CollectionItem | None = None
    revealedCell: Position | None = None


class Result(Model):
    runId: UUID
    shiftId: ShiftId
    reason: Literal["goal_reached", "moves_exhausted", "early_finish"]
    resourceId: ResourceId | None
    collected: Natural
    bonus: Natural
    total: Natural
    credited: Natural
    overflow: Natural
    goalReached: bool
    warehouse: Warehouse
    dig: DigResult | None = None


class Profile(Model):
    schemaVersion: Literal[1] = 1
    stock: Resources
    limits: Resources
    workers: dict[ShiftId, Natural]
    energy: dict[ShiftId, bool]
    happinessMoves: Natural = 2
    forestMoves: Natural = 2
    lastResult: Result | None = None


class Run(Model):
    schemaVersion: Literal[1] = 1
    rules: Rules
    board: Board
    rngState: Annotated[int, Field(ge=1, le=2**32 - 1)]
    movesLeft: Natural
    chargesLeft: Natural
    collected: Natural = 0
    digDepth: Annotated[int, Field(ge=0, le=6)] = 0
    siteId: str | None = None
    revision: Natural = 0
    status: Literal["active", "finished"] = "active"
    result: Result | None = None
    settledAt: Timestamp | None = None

    @model_validator(mode="after")
    def valid_run(self) -> Self:
        if len(self.board) != 8 or any(len(row) != 8 for row in self.board):
            raise ValueError("Поле должно иметь размер 8×8")
        if any(chip not in self.rules.chips for row in self.board for chip in row):
            raise ValueError("Фишка не относится к этой смене")
        if self.movesLeft > self.rules.moves or self.chargesLeft > self.rules.charges:
            raise ValueError("Неверные остатки ходов или зарядов")
        if (self.status == "finished") != (self.result is not None):
            raise ValueError("Итог не соответствует состоянию смены")
        if self.rules.shiftId == "dig" and self.siteId is None:
            raise ValueError("Раскопка не привязана к участку")
        return self

    @property
    def amphora(self) -> CellPosition | None:
        if self.rules.shiftId != "dig":
            return None
        return CellPosition(row=1 + self.digDepth, column=3)

    @property
    def goal_reached(self) -> bool:
        return (
            self.digDepth >= 6
            if self.rules.shiftId == "dig"
            else self.collected >= self.rules.target
        )


class CascadeStep(Model):
    kind: Literal["match", "lightning", "reshuffle"]
    before: Board
    cleared: list[CellPosition]
    after: Board
    earned: Natural = 0
    amphoraBefore: CellPosition | None = None
    amphoraAfter: CellPosition | None = None


class RunView(Model):
    playerId: UUID
    serverSavedAt: Timestamp
    runId: UUID
    shiftId: ShiftId
    board: Board
    goal: Goal
    movesLeft: Natural
    chargesLeft: Natural
    collected: Natural
    loot: Resources
    digDepth: int
    amphora: CellPosition | None
    revision: Natural
    status: Literal["active", "finished"]
    result: Result | None


class MoveResponse(RunView):
    accepted: bool
    rejectionReason: str | None = None
    swappedBoard: Board
    steps: list[CascadeStep]


class LightningResponse(RunView):
    cleared: list[CellPosition]
    steps: list[CascadeStep]


class FinishResponse(Result):
    playerId: UUID
    serverSavedAt: Timestamp


class HintResponse(Model):
    playerId: UUID
    runId: UUID
    revision: Natural
    available: bool
    from_: CellPosition | None = Field(alias="from")
    to: CellPosition | None


class ShiftView(Model):
    shiftId: ShiftId
    title: str
    resourceId: ResourceId | None
    goal: Goal
    moves: int
    charges: int
    requiredWorkers: int
    assignedWorkers: int
    available: bool
    reasons: list[Reason]


class ExcavationView(Model):
    siteId: str
    position: Position
    terrain: str
    depth: int
    targetDepth: int = 6
    paid: bool


class StateResponse(Model):
    playerId: UUID
    serverSavedAt: Timestamp
    warehouse: Warehouse
    digDepth: int
    excavation: ExcavationView | None
    shifts: list[ShiftView]
    activeRun: RunView | None
    lastResult: Result | None


class StartRequest(Model):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {"shiftId": "sawmill"},
                {"shiftId": "dig"},
                {"shiftId": "dig", "target": {"q": 4, "r": 0}},
            ]
        }
    )
    shiftId: ShiftId
    target: Position | None = None

    @model_validator(mode="after")
    def target_only_for_dig(self) -> Self:
        if self.target is not None and self.shiftId != "dig":
            raise ValueError("Участок тумана задаётся только для раскопок")
        return self


class MoveRequest(Model):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {"from": {"row": 0, "column": 0}, "to": {"row": 0, "column": 1}}
            ]
        }
    )
    from_: CellPosition = Field(alias="from")
    to: CellPosition
    expectedRevision: Natural | None = None


class LightningRequest(Model):
    model_config = ConfigDict(json_schema_extra={"examples": [{"row": 3}]})
    row: Annotated[int, Field(ge=0, le=7)]
    expectedRevision: Natural | None = None


class FinishRequest(Model):
    expectedRevision: Natural | None = None
