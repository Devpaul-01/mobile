# BirthdayMessaging Mobile Backend

New, standalone FastAPI backend for the mobile app. Separate codebase, separate
database, separate deployment from `bm-ts/backend` (the Chrome extension's
backend). See `MOBILE_APP_ARCHITECTURE_SPEC.md` for the full architecture and
rationale — in particular §12.5 for why this is a separate service rather than
routers bolted onto the existing backend.

## Local development

```bash
cp .env.example .env    # fill in real values — see comments in the file
docker-compose up
```

This starts Postgres, Redis, and the backend with Alembic migrations applied
automatically and hot-reload enabled.

## Running tests

```bash
pip install -e ".[test]"
pytest
```

Tests run against an in-memory SQLite database (see `app/tests/conftest.py`),
not a real Postgres instance — no local database needed to run the suite.

## Migrations

```bash
alembic revision --autogenerate -m "description"
alembic upgrade head
```
