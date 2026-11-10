from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.models import AuthSession, LoginBucket, User


class AuthRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def user_by_email(self, email: str) -> User | None:
        return self.db.scalar(select(User).where(User.email == email))

    def user(self, user_id: UUID) -> User | None:
        return self.db.get(User, user_id)

    def add_user(self, email: str, password_hash: str) -> User:
        user = User(email=email, password_hash=password_hash)
        self.db.add(user)
        self.db.flush()
        return user

    def add_session(self, session: AuthSession) -> None:
        self.db.add(session)

    def session(self, session_id: UUID, *, lock: bool = False) -> AuthSession | None:
        query = select(AuthSession).where(AuthSession.id == session_id)
        if lock:
            query = query.with_for_update()
        return self.db.scalar(query)

    def consume_attempt(self, key: str, now: datetime, window: int, limit: int) -> bool:
        """Serialize each IP bucket across workers; persist failed attempts too."""
        self.db.execute(
            insert(LoginBucket)
            .values(key=key, attempts=0, expires_at=now + timedelta(seconds=window))
            .on_conflict_do_nothing()
        )
        bucket = self.db.scalars(
            select(LoginBucket).where(LoginBucket.key == key).with_for_update()
        ).one()
        if bucket.expires_at <= now:
            bucket.attempts = 0
            bucket.expires_at = now + timedelta(seconds=window)
        allowed = bucket.attempts < limit
        if allowed:
            bucket.attempts += 1
        self.db.commit()
        return allowed

    def commit(self) -> None:
        self.db.commit()

    def rollback(self) -> None:
        self.db.rollback()
