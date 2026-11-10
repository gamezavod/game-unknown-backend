"""Run migrations and integration tests against an explicitly isolated database."""

import os
import subprocess

from dotenv import load_dotenv
from sqlalchemy.engine import make_url

load_dotenv()
url = os.environ["TEST_DATABASE_URL"]
if not (make_url(url).database or "").endswith("_test"):
    raise SystemExit(
        "TEST_DATABASE_URL database must end in _test; tests truncate tables"
    )
environment = {**os.environ, "DATABASE_URL": url}
subprocess.run([".venv/bin/alembic", "upgrade", "head"], env=environment, check=True)
subprocess.run([".venv/bin/alembic", "check"], env=environment, check=True)
subprocess.run([".venv/bin/pytest", "-q"], env=environment, check=True)
