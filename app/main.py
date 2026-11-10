from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

from app.config import get_settings
from app.routes import router
from app.services import AuthError


def create_app() -> FastAPI:
    get_settings()
    app = FastAPI(title="GameZavod API")
    app.include_router(router)

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
