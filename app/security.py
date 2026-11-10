import hashlib
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

from app.config import Settings

password_hasher = PasswordHasher()
DUMMY_HASH = password_hasher.hash("constant-time-dummy-password")


def verify_password(hashed: str, password: str) -> bool:
    try:
        return password_hasher.verify(hashed, password)
    except (VerificationError, InvalidHashError):
        return False


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class Tokens:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def encode(self, user: UUID, session: UUID, kind: str, ttl: int) -> str:
        now = datetime.now(UTC)
        return jwt.encode(
            {
                "sub": str(user),
                "sid": str(session),
                "type": kind,
                "jti": str(uuid4()),
                "iat": now,
                "exp": now + timedelta(seconds=ttl),
                "iss": self.settings.jwt_issuer,
                "aud": self.settings.jwt_audience,
            },
            self.settings.jwt_secret.get_secret_value(),
            algorithm=self.settings.jwt_algorithm,
        )

    def decode(self, token: str, kind: str) -> dict[str, Any]:
        claims: dict[str, Any] = jwt.decode(
            token,
            self.settings.jwt_secret.get_secret_value(),
            algorithms=[self.settings.jwt_algorithm],
            issuer=self.settings.jwt_issuer,
            audience=self.settings.jwt_audience,
            options={"require": ["sub", "sid", "type", "jti", "iat", "exp"]},
        )
        if claims["type"] != kind:
            raise jwt.InvalidTokenError("Invalid token type")
        if not isinstance(claims["sid"], str):
            raise jwt.InvalidTokenError("Invalid session claim")
        UUID(claims["sub"])
        UUID(claims["sid"])
        return claims
