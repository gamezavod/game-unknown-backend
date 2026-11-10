from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from uuid import UUID

import jwt
import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_engine
from app.dependencies import require_admin
from app.models import AuthSession, User
from app.security import digest, password_hasher

DATA = {"email": "player@example.com", "password": "a-long-test-password"}


def login(client: TestClient) -> dict[str, str]:
    assert client.post("/auth/register", json=DATA).status_code == 201
    response = client.post("/auth/login", json=DATA)
    assert response.status_code == 200
    return response.json()  # type: ignore[no-any-return]


def bearer(tokens: dict[str, str]) -> dict[str, str]:
    return {"Authorization": "Bearer " + tokens["access_token"]}


def test_registration_duplicate_validation(client: TestClient) -> None:
    response = client.post("/auth/register", json=DATA)
    assert response.status_code == 201
    assert response.json()["role"] == "user"
    assert "password" not in response.text
    assert client.post("/auth/register", json=DATA).status_code == 409
    assert (
        client.post(
            "/auth/register", json={**DATA, "email": "PLAYER@example.com"}
        ).status_code
        == 409
    )
    assert (
        client.post("/auth/register", json={**DATA, "password": "short"}).status_code
        == 422
    )
    assert (
        client.post("/auth/register", json={**DATA, "email": "invalid"}).status_code
        == 422
    )
    assert (
        client.post("/auth/register", json={**DATA, "role": "admin"}).status_code == 422
    )
    with Session(get_engine()) as db:
        user = db.scalars(select(User)).one()
        assert user.password_hash != DATA["password"]
        assert password_hasher.verify(user.password_hash, DATA["password"])


def test_full_flow(client: TestClient) -> None:
    tokens = login(client)
    headers = bearer(tokens)
    assert client.get("/me", headers=headers).json()["email"] == DATA["email"]
    assert client.get("/me").status_code == 401
    assert (
        client.get("/me", headers={"Authorization": "Bearer invalid"}).status_code
        == 401
    )
    assert (
        client.get(
            "/me", headers={"Authorization": "Bearer " + tokens["refresh_token"]}
        ).status_code
        == 401
    )
    refreshed = client.post(
        "/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
    )
    assert refreshed.status_code == 200
    new = refreshed.json()
    assert new["refresh_token"] != tokens["refresh_token"]
    assert (
        client.post(
            "/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
        ).status_code
        == 401
    )
    assert (
        client.post(
            "/auth/logout", json={"refresh_token": new["refresh_token"]}
        ).status_code
        == 204
    )
    assert client.get("/me", headers=bearer(new)).status_code == 401
    assert client.get("/me", headers=headers).status_code == 401
    assert (
        client.post(
            "/auth/refresh", json={"refresh_token": new["refresh_token"]}
        ).status_code
        == 401
    )
    assert (
        client.post(
            "/auth/logout", json={"refresh_token": new["refresh_token"]}
        ).status_code
        == 401
    )


def test_invalid_credentials_and_limit(client: TestClient) -> None:
    login(client)
    wrong = client.post("/auth/login", json={**DATA, "password": "wrong-password"})
    absent = client.post("/auth/login", json={**DATA, "email": "absent@example.com"})
    assert wrong.status_code == absent.status_code == 401
    assert wrong.json() == absent.json() == {"detail": "Invalid credentials"}
    for _ in range(2):
        assert client.post("/auth/login", json=DATA).status_code == 200
    limited = client.post("/auth/login", json=DATA)
    assert limited.status_code == 429
    assert "Retry-After" in limited.headers


@pytest.mark.parametrize(
    "kind", ["expired", "signature", "audience", "issuer", "subject"]
)
def test_bad_access(client: TestClient, kind: str) -> None:
    tokens = login(client)
    settings = get_settings()
    claims = jwt.decode(tokens["access_token"], options={"verify_signature": False})
    key = settings.jwt_secret.get_secret_value()
    if kind == "expired":
        claims["exp"] = datetime.now(UTC) - timedelta(seconds=1)
    elif kind == "signature":
        key = "x" * 48
    elif kind == "audience":
        claims["aud"] = "other"
    elif kind == "issuer":
        claims["iss"] = "other"
    else:
        claims["sub"] = "not-a-uuid"
    token = jwt.encode(claims, key, algorithm=settings.jwt_algorithm)
    assert (
        client.get("/me", headers={"Authorization": "Bearer " + token}).status_code
        == 401
    )


def test_refresh_hash_expiration_and_types(client: TestClient) -> None:
    tokens = login(client)
    with Session(get_engine()) as db:
        session = db.scalars(select(AuthSession)).one()
        assert session.refresh_hash == digest(tokens["refresh_token"])
        session.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        db.commit()
    assert (
        client.post(
            "/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
        ).status_code
        == 401
    )
    assert (
        client.post(
            "/auth/refresh", json={"refresh_token": tokens["access_token"]}
        ).status_code
        == 401
    )
    assert (
        client.post("/auth/logout", json={"refresh_token": "invalid"}).status_code
        == 401
    )


def test_concurrent_refresh_single_winner(client: TestClient) -> None:
    tokens = login(client)

    def refresh(_: int) -> int:
        response = client.post(
            "/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
        )
        return int(response.status_code)

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(refresh, range(2))) == [200, 401]


def test_role_guard(client: TestClient) -> None:
    assert isinstance(client.app, FastAPI)

    @client.app.get("/admin", dependencies=[Depends(require_admin)])
    def admin() -> dict[str, bool]:
        return {"ok": True}

    tokens = login(client)
    assert client.get("/admin", headers=bearer(tokens)).status_code == 403
    assert client.get("/admin").status_code == 401
    with Session(get_engine()) as db:
        user = db.get(
            User, UUID(client.get("/me", headers=bearer(tokens)).json()["id"])
        )
        assert user is not None
        user.role = "admin"
        db.commit()
    assert client.get("/admin", headers=bearer(tokens)).status_code == 200


def test_expired_refresh_jwt(client: TestClient) -> None:
    tokens = login(client)
    settings = get_settings()
    claims = jwt.decode(tokens["refresh_token"], options={"verify_signature": False})
    claims["exp"] = datetime.now(UTC) - timedelta(seconds=1)
    expired = jwt.encode(
        claims, settings.jwt_secret.get_secret_value(), algorithm=settings.jwt_algorithm
    )
    with Session(get_engine()) as db:
        session = db.scalars(select(AuthSession)).one()
        session.refresh_hash = digest(expired)
        db.commit()
    assert (
        client.post("/auth/refresh", json={"refresh_token": expired}).status_code == 401
    )


def test_logout_only_current_session(client: TestClient) -> None:
    first = login(client)
    second = client.post("/auth/login", json=DATA).json()
    assert (
        client.post(
            "/auth/logout", json={"refresh_token": first["refresh_token"]}
        ).status_code
        == 204
    )
    assert client.get("/me", headers=bearer(second)).status_code == 200
