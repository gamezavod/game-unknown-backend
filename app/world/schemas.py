from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field

from app.world.state import Model, Position, Timestamp


class OpenWorld(Model):
    seed: str | None = None
    initialSave: str | None = None


class PositionedAction(Model):
    type: Literal["reveal", "chopForest", "plantForest"]
    position: Position
    # Принимаются для совместимости DTO, но не влияют на серверные часы.
    now: Timestamp | None = None
    growthDurationMs: Timestamp | None = None


class TimedAction(Model):
    type: Literal["advanceGrowth", "completeShift"]
    now: Timestamp | None = None


class ActionRequest(Model):
    action: Annotated[PositionedAction | TimedAction, Field(discriminator="type")]
    expectedServerSavedAt: Timestamp | None


class SaveRequest(Model):
    data: str
    expectedServerSavedAt: Timestamp | None


class StoredSave(Model):
    playerId: UUID
    data: str
    serverSavedAt: Timestamp
