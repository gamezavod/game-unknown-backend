from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db import get_db
from app.dependencies import current_user
from app.models import User
from app.world.repository import WorldRepository
from app.world.schemas import ActionRequest, OpenWorld, SaveRequest, StoredSave
from app.world.service import WorldService

router = APIRouter(prefix="/game", tags=["Мир и прогресс"])


def get_world_service(
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> WorldService:
    return WorldService(WorldRepository(db), settings)


Service = Annotated[WorldService, Depends(get_world_service)]
Player = Annotated[User, Depends(current_user)]


@router.post("/session", response_model=StoredSave)
def open_world(body: OpenWorld, user: Player, service: Service) -> StoredSave:
    return service.open(user.id, body)


@router.get("/progress", response_model=StoredSave)
def load_progress(user: Player, service: Service) -> StoredSave:
    return service.load(user.id)


@router.put("/progress", response_model=StoredSave)
def save_progress(body: SaveRequest, user: Player, service: Service) -> StoredSave:
    return service.replace(user.id, body)


@router.post("/actions", response_model=StoredSave)
def world_action(body: ActionRequest, user: Player, service: Service) -> StoredSave:
    return service.action(user.id, body)
