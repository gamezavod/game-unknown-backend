import hmac
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import jwt
from sqlalchemy.exc import IntegrityError

from app.config import Settings
from app.models import AuthSession, User
from app.repositories import AuthRepository
from app.schemas import Credentials, TokenPair
from app.security import DUMMY_HASH, Tokens, digest, password_hasher, verify_password


class AuthError(Exception):
    def __init__(self, status: int, detail: str) -> None:
        self.status = status
        self.detail = detail


class AuthService:
    def __init__(self, repo: AuthRepository, settings: Settings) -> None:
        self.repo = repo
        self.settings = settings
        self.tokens = Tokens(settings)

    def register(self, data: Credentials) -> User:
        hashed = password_hasher.hash(data.password)
        try:
            user = self.repo.add_user(str(data.email), hashed)
            self.repo.commit()
            return user
        except IntegrityError:
            self.repo.rollback()
            raise AuthError(409, "Registration unavailable") from None

    def login(self, data: Credentials, client_ip: str) -> TokenPair:
        allowed = self.repo.consume_attempt(
            digest(client_ip),
            datetime.now(UTC),
            self.settings.login_window_seconds,
            self.settings.login_max_attempts,
        )
        if not allowed:
            raise AuthError(429, "Too many login attempts")
        user = self.repo.user_by_email(str(data.email))
        valid = verify_password(
            user.password_hash if user else DUMMY_HASH, data.password
        )
        if user is None or not valid:
            raise AuthError(401, "Invalid credentials")
        session = AuthSession(
            id=uuid4(),
            user_id=user.id,
            expires_at=datetime.now(UTC)
            + timedelta(seconds=self.settings.refresh_ttl_seconds),
        )
        pair = self.issue(session)
        self.repo.add_session(session)
        self.repo.commit()
        return pair

    def issue(self, session: AuthSession) -> TokenPair:
        refresh = self.tokens.encode(
            session.user_id, session.id, "refresh", self.settings.refresh_ttl_seconds
        )
        session.refresh_hash = digest(refresh)
        return TokenPair(
            access_token=self.tokens.encode(
                session.user_id, session.id, "access", self.settings.access_ttl_seconds
            ),
            refresh_token=refresh,
            expires_in=self.settings.access_ttl_seconds,
        )

    def authenticate(self, token: str, kind: str, *, lock: bool = False) -> AuthSession:
        try:
            claims = self.tokens.decode(token, kind)
            session = self.repo.session(UUID(claims["sid"]), lock=lock)
            if (
                session is None
                or session.revoked_at is not None
                or session.expires_at <= datetime.now(UTC)
                or str(session.user_id) != claims["sub"]
            ):
                raise ValueError
            if kind == "refresh" and not hmac.compare_digest(
                session.refresh_hash, digest(token)
            ):
                raise ValueError
            return session
        except (jwt.InvalidTokenError, ValueError, TypeError, KeyError):
            self.repo.rollback()
            raise AuthError(401, "Invalid or expired token") from None

    def current_user(self, token: str) -> User:
        session = self.authenticate(token, "access")
        user = self.repo.user(session.user_id)
        if user is None:
            raise AuthError(401, "Invalid or expired token")
        return user

    def refresh(self, token: str) -> TokenPair:
        session = self.authenticate(token, "refresh", lock=True)
        pair = self.issue(session)
        self.repo.commit()
        return pair

    def logout(self, token: str) -> None:
        session = self.authenticate(token, "refresh", lock=True)
        session.revoked_at = datetime.now(UTC)
        self.repo.commit()
