# GameZavod

FastAPI backend, Python 3.12+, PostgreSQL 17, SQLAlchemy 2, Alembic, uv.
Dependencies are pinned in `uv.lock`. No existing application code was present.

## Local development

Install [uv](https://docs.astral.sh/uv/), Docker and Docker Compose. From the
repository root:

```sh
cp .env.example .env
# Fill POSTGRES_PASSWORD and JWT_SECRET with independently generated random values.
# Generate each with: python -c 'import secrets; print(secrets.token_urlsafe(48))'
# Set DATABASE_URL to match the PostgreSQL user/password/database (URL-encode password).
uv sync --frozen
docker compose up -d --wait
uv run alembic upgrade head
uv run uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8000
```

Swagger is at `/docs`, schema at `/openapi.json`. Use Swagger's **Authorize**
button with the access token from login. Login accepts JSON, not OAuth form data.
The application validates required configuration at startup and never creates
schema automatically. Run migrations before starting workers.

`.env` is ignored. Never commit credentials. Supply environment variables directly
in deployed environments; `.env` is a local convenience. JWT_SECRET is required
(minimum 32 characters; use cryptographically random data). Supported algorithms
are HS256/HS384/HS512; all consumers must share the secret securely. Defaults:
access 900 seconds, refresh/session 2592000 seconds, issuer `gamezavod`, audience
`gamezavod-api`, 5 logins per source IP per 300 seconds. All settings are exposed
in `.env.example`. Rotate the signing secret to invalidate all tokens.

## API and architecture

- `POST /auth/register`: `{ "email": "player@example.com", "password": "..." }`, 201.
- `POST /auth/login`: same body, 200 access/refresh JWT pair.
- `GET /me`: `Authorization: Bearer <access_token>`, 200 public user fields.
- `POST /auth/refresh`: `{ "refresh_token": "..." }`, 200 rotated pair.
- `POST /auth/logout`: same body, 204; session access and refresh now return 401.

`routes.py` maps HTTP to `services.py`; `repositories.py` owns database access;
`models.py` and `schemas.py` define persistence and public validation.
`dependencies.py` supplies injectable sessions/services and `current_user` /
`require_admin` guards. Registration always sets role `user`; there is no public
role assignment endpoint. `security.py` handles Argon2 and typed JWT claims.

Refresh JWTs are stored only as SHA-256 digests. Row locks serialize rotation and
logout. Rotation is single-use; old refresh tokens return 401, without revoking
an otherwise valid session. Session lifetime is fixed from login (not sliding).
Access tokens are checked against session state on every request, so logout
invalidates them immediately. Logout accepts the latest refresh token and affects
only that login session. Repeated logout returns 401.

Errors use `{ "detail": ... }`; validation errors use FastAPI's standard detail
list. Statuses: 401 authentication, 403 role, 409 registration conflict, 422 input,
429 rate limit. Missing users and wrong passwords share the same 401 response
and perform Argon2 verification. Email is normalized to lowercase. Passwords
are 12–128 characters; request schemas forbid extra fields. Registration conflicts
use a generic message, but a 409 necessarily reveals registration availability.

Login rate limits use PostgreSQL row locks and are shared across workers. All
attempts count, including successful logins. Client IP comes from the ASGI peer;
configure trusted proxy IPs explicitly when deploying behind a reverse proxy.
Do not trust arbitrary forwarded headers. Apply upstream request-size and
registration/global rate limits as well. Use TLS in production, and keep browser
tokens out of localStorage where possible. This JSON bearer API does not set
cookies; cookie-based integration would require a separate CSRF design.

## Validation

Create a dedicated test database (tests truncate their tables):

```sh
docker compose exec -T db createdb -U gamezavod gamezavod_test
# Set TEST_DATABASE_URL with the same credentials and database gamezavod_test.
export TEST_DATABASE_URL='postgresql+psycopg://USER:PASSWORD@localhost:5432/gamezavod_test'
uv run python scripts/check.py
uv run ruff check .
uv run ruff format --check .
uv run mypy app tests migrations scripts
# Against the running app; creates a uniquely named smoke user/session:
uv run python scripts/smoke.py
```

Test database names must end in `_test`. The check script applies migrations,
checks model/schema drift and runs PostgreSQL integration tests. CI runs the same
checks against PostgreSQL 17. `alembic downgrade base` is destructive: use only
on disposable databases. For new schema changes use
`uv run alembic revision --autogenerate -m 'description'` and review the migration.

## Scope and operational follow-up

Initial assumptions: email/password login, public registration, JSON JWT pairs,
fixed-duration per-login sessions, no existing user model or framework conventions.
Email verification, password reset, MFA, OAuth providers, global logout, refresh
replay family detection and admin management are outside this change.
Schedule deletion of expired/revoked sessions and expired login buckets in
production; no background scheduler is included. Back up PostgreSQL and provide
monitoring, TLS termination and secret management before public deployment.
