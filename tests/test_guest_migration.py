import os
import subprocess
import sys
from uuid import uuid4

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from app.world.progress import new_progress
from app.world.state import SaveGame


def test_guest_migration_preserves_existing_saves_and_refuses_data_loss() -> None:
    url = make_url(os.environ["TEST_DATABASE_URL"])
    if not (url.database or "").endswith("_test"):
        raise RuntimeError("Migration tests require an isolated _test database")
    schema = "guest_migration_" + uuid4().hex
    base = create_engine(url)
    scoped = create_engine(
        url.update_query_dict({"options": f"-csearch_path={schema}"})
    )
    with base.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    environment = {
        **os.environ,
        "DATABASE_URL": scoped.url.render_as_string(hide_password=False),
    }

    def migrate(command: str, target: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "alembic", command, target],
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )

    try:
        result = migrate("upgrade", "002_world_progress")
        assert result.returncode == 0, result.stderr
        user_id = uuid4()
        saved = SaveGame(
            savedAt=100,
            serverSavedAt=101,
            revision=3,
            cloudPending=False,
            progress=new_progress("existing-account"),
        )
        with scoped.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO users (id, email, password_hash, role) "
                    "VALUES (:id, 'migration@example.com', 'unused', 'user')"
                ),
                {"id": user_id},
            )
            connection.execute(
                text(
                    "INSERT INTO world_saves (user_id, data, server_saved_at) "
                    "VALUES (:id, cast(:data AS jsonb), 101)"
                ),
                {"id": user_id, "data": saved.model_dump_json()},
            )
        result = migrate("upgrade", "head")
        assert result.returncode == 0, result.stderr
        with scoped.begin() as connection:
            row = connection.execute(
                text(
                    "SELECT player_id, user_id, data, server_saved_at FROM world_saves"
                )
            ).one()
            assert row.player_id == row.user_id == user_id
            assert row.data == saved.model_dump()
            assert row.server_saved_at == 101
            connection.execute(
                text(
                    "INSERT INTO world_saves (player_id, data, server_saved_at) "
                    "VALUES (:id, cast(:data AS jsonb), 101)"
                ),
                {"id": uuid4(), "data": saved.model_dump_json()},
            )
        result = migrate("downgrade", "002_world_progress")
        assert result.returncode != 0
        assert "гостевые сохранения" in result.stderr
        with scoped.connect() as connection:
            assert connection.scalar(text("SELECT count(*) FROM world_saves")) == 2
    finally:
        scoped.dispose()
        with base.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        base.dispose()
