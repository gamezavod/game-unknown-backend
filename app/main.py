from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

from app.config import get_settings
from app.match3.routes import router as match3_router
from app.match3.service import Match3Error
from app.routes import router
from app.services import AuthError
from app.world.routes import router as world_router
from app.world.service import WorldError


def create_app() -> FastAPI:
    get_settings()
    app = FastAPI(title="GameZavod API")
    app.include_router(router)
    app.include_router(world_router)
    app.include_router(match3_router)

    @app.exception_handler(Match3Error)
    async def match3_error(request: Request, exc: Match3Error) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status, content={"detail": exc.detail, "code": exc.code}
        )

    @app.exception_handler(WorldError)
    async def world_error(request: Request, exc: WorldError) -> JSONResponse:
        content: dict[str, object] = {"detail": exc.detail}
        if exc.current is not None:
            content["current"] = exc.current.model_dump(mode="json")
        return JSONResponse(status_code=exc.status, content=content)

    @app.exception_handler(AuthError)
    async def auth_error(request: Request, exc: AuthError) -> JSONResponse:
        headers = {"WWW-Authenticate": "Bearer"} if exc.status == 401 else {}
        if exc.status == 429:
            headers["Retry-After"] = str(get_settings().login_window_seconds)
        return JSONResponse(
            status_code=exc.status, content={"detail": exc.detail}, headers=headers
        )

    @app.middleware("http")
    async def no_store(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        return response

    return app
