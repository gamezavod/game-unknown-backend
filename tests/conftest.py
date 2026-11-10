import os
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from app.config import get_settings
from app.db import get_engine
from app.main import create_app


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    url = os.environ["TEST_DATABASE_URL"]
    if not (make_url(url).database or "").endswith("_test"):
        raise RuntimeError("Tests require a database name ending in _test")
    monkeypatch.setenv("DATABASE_URL", url)
    get_settings.cache_clear()
    get_engine.cache_clear()
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE auth_sessions, users, login_buckets CASCADE"))
    with TestClient(create_app()) as client:
        yield client
    get_engine().dispose()
    engine.dispose()
    get_engine.cache_clear()
    get_settings.cache_clear()
