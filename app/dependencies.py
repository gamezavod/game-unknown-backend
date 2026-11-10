from typing import Annotated

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db import get_db
from app.models import User
from app.repositories import AuthRepository
from app.services import AuthError, AuthService

bearer = HTTPBearer(auto_error=False)


def get_service(
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> AuthService:
    return AuthService(AuthRepository(db), settings)


Service = Annotated[AuthService, Depends(get_service)]


def current_user(
    service: Service,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> User:
    if credentials is None:
        raise AuthError(401, "Authentication required")
    return service.current_user(credentials.credentials)


def require_admin(user: Annotated[User, Depends(current_user)]) -> User:
    if user.role != "admin":
        raise AuthError(403, "Insufficient permissions")
    return user
