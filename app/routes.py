from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response

from app.dependencies import Service, current_user
from app.models import User
from app.schemas import Credentials, RefreshRequest, TokenPair, UserResponse

router = APIRouter()


@router.post("/auth/register", response_model=UserResponse, status_code=201)
def register(data: Credentials, service: Service) -> User:
    return service.register(data)


@router.post("/auth/login", response_model=TokenPair)
def login(data: Credentials, request: Request, service: Service) -> TokenPair:
    return service.login(data, request.client.host if request.client else "unknown")


@router.post("/auth/refresh", response_model=TokenPair)
def refresh(data: RefreshRequest, service: Service) -> TokenPair:
    return service.refresh(data.refresh_token)


@router.post("/auth/logout", status_code=204)
def logout(data: RefreshRequest, service: Service) -> Response:
    service.logout(data.refresh_token)
    return Response(status_code=204)


@router.get("/me", response_model=UserResponse)
def me(user: Annotated[User, Depends(current_user)]) -> User:
    return user
