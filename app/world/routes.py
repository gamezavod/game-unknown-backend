from typing import Annotated
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db import get_db
from app.world.repository import WorldRepository
from app.world.schemas import ActionRequest, OpenWorld, SaveRequest, StoredSave
from app.world.service import WorldService

router = APIRouter(prefix="/game", tags=["Мир и прогресс"])
PLAYER_COOKIE = "game_player_id"


def get_world_service(
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> WorldService:
    return WorldService(WorldRepository(db), settings)


def current_player(
    request: Request,
    player_id: Annotated[UUID | None, Query()] = None,
) -> UUID:
    """Публичный выбор мира, без проверки JWT или прав доступа."""
    if player_id is not None:
        return player_id
    cookie = request.cookies.get(PLAYER_COOKIE)
    if cookie:
        try:
            return UUID(cookie)
        except ValueError:
            pass
    return uuid4()


Service = Annotated[WorldService, Depends(get_world_service)]
Player = Annotated[UUID, Depends(current_player)]


@router.post("/session", response_model=StoredSave)
def open_world(
    body: OpenWorld, player: Player, service: Service, response: Response
) -> StoredSave:
    saved = service.open(player, body)
    response.set_cookie(
        PLAYER_COOKIE,
        str(player),
        max_age=365 * 24 * 3600,
        httponly=True,
        samesite="lax",
        path="/game",
    )
    return saved


@router.get("/progress", response_model=StoredSave)
def load_progress(player: Player, service: Service) -> StoredSave:
    return service.load(player)


@router.put("/progress", response_model=StoredSave)
def save_progress(body: SaveRequest, player: Player, service: Service) -> StoredSave:
    return service.replace(player, body)


@router.post("/actions", response_model=StoredSave)
def world_action(body: ActionRequest, player: Player, service: Service) -> StoredSave:
    return service.action(player, body)
