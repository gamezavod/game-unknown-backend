from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db import get_db
from app.match3.repository import Match3Repository
from app.match3.service import Match3Service
from app.match3.state import (
    FinishRequest,
    FinishResponse,
    HintResponse,
    LightningRequest,
    LightningResponse,
    MoveRequest,
    MoveResponse,
    RunView,
    StartRequest,
    StateResponse,
)
from app.world.routes import Player

router = APIRouter(prefix="/game/match3", tags=["Рабочие смены match-3"])


def get_service(
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> Match3Service:
    return Match3Service(Match3Repository(db), settings)


Service = Annotated[Match3Service, Depends(get_service)]
Key = Annotated[
    str | None, Header(alias="Idempotency-Key", min_length=1, max_length=128)
]


@router.get("/state", response_model=StateResponse)
def state(player: Player, service: Service) -> StateResponse:
    return service.state(player)


@router.post("/runs", response_model=RunView, status_code=201)
def start(
    body: StartRequest, player: Player, service: Service, key: Key = None
) -> RunView:
    return service.start(player, body.shiftId, key, body.target)


@router.post("/runs/{run_id}/moves", response_model=MoveResponse)
def move(
    run_id: UUID, body: MoveRequest, player: Player, service: Service, key: Key = None
) -> MoveResponse:
    return service.play(player, run_id, body, key)


@router.get("/runs/{run_id}/hint", response_model=HintResponse)
def hint(run_id: UUID, player: Player, service: Service) -> HintResponse:
    return service.hint(player, run_id)


@router.post("/runs/{run_id}/lightning", response_model=LightningResponse)
def lightning(
    run_id: UUID,
    body: LightningRequest,
    player: Player,
    service: Service,
    key: Key = None,
) -> LightningResponse:
    return service.strike(player, run_id, body.row, body.expectedRevision, key)


@router.post("/runs/{run_id}/finish", response_model=FinishResponse)
def finish(
    run_id: UUID,
    player: Player,
    service: Service,
    body: FinishRequest | None = None,
    key: Key = None,
) -> FinishResponse:
    return service.finish(player, run_id, body.expectedRevision if body else None, key)
