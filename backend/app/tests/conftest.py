"""
conftest.py — Shared test fixtures.

Pattern copied from bm-ts/backend/app/tests/conftest.py: an in-memory SQLite
database (no real Postgres needed to run the suite), with Postgres-specific
server_default SQL stripped before create_all since SQLite can't parse it.
"""

from __future__ import annotations

import os

# Must be set before app.core.config is imported anywhere (Settings() runs
# its fail-fast validators at import time).
os.environ.setdefault(
    "JWT_SECRET_KEY", "ci-test-only-secret-not-for-production-minimum32chars"
)
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
# Importing app.main starts the APScheduler job registration in its
# lifespan handler on app startup; the test suite never runs a real
# lifespan cycle against the per-test SQLite engine, but async_client
# below does trigger app import, so keep the scheduler disabled here too
# as a second line of defense.
os.environ.setdefault("SCHEDULER_ENABLED", "false")

import uuid
from datetime import datetime, timezone
from typing import AsyncGenerator

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import StaticPool, event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.base import Base
from app.db.models import (
    ApprovalChannel,
    ApprovalHistory,
    ApprovalState,
    Contact,
    ContactIdentifier,
    ContactPhoto,
    EmailVerificationToken,
    PasswordResetToken,
    PendingApproval,
    PushToken,
    Reminder,
    ReminderOccurrence,
    RevokedToken,
    User,
    UserSession,
)
from app.core.security import hash_password

from sqlalchemy import event as sa_event


@sa_event.listens_for(User, "before_insert")
def _set_user_defaults(mapper, connection, target):
    now = datetime.now(timezone.utc)
    if getattr(target, "created_at", None) is None:
        target.created_at = now
    if getattr(target, "updated_at", None) is None:
        target.updated_at = now
    if getattr(target, "failed_login_count", None) is None:
        target.failed_login_count = 0
    if getattr(target, "email_verified", None) is None:
        target.email_verified = False


@sa_event.listens_for(UserSession, "before_insert")
def _set_session_defaults(mapper, connection, target):
    now = datetime.now(timezone.utc)
    if getattr(target, "created_at", None) is None:
        target.created_at = now
    if getattr(target, "last_seen_at", None) is None:
        target.last_seen_at = now


@sa_event.listens_for(RevokedToken, "before_insert")
def _set_revoked_at(mapper, connection, target):
    if getattr(target, "revoked_at", None) is None:
        target.revoked_at = datetime.now(timezone.utc)


@sa_event.listens_for(EmailVerificationToken, "before_insert")
def _set_email_verif_created_at(mapper, connection, target):
    if getattr(target, "created_at", None) is None:
        target.created_at = datetime.now(timezone.utc)


@sa_event.listens_for(PasswordResetToken, "before_insert")
def _set_reset_created_at(mapper, connection, target):
    if getattr(target, "created_at", None) is None:
        target.created_at = datetime.now(timezone.utc)


@sa_event.listens_for(Contact, "before_insert")
def _set_contact_defaults(mapper, connection, target):
    now = datetime.now(timezone.utc)
    if getattr(target, "created_at", None) is None:
        target.created_at = now
    if getattr(target, "updated_at", None) is None:
        target.updated_at = now


@sa_event.listens_for(ContactIdentifier, "before_insert")
def _set_contact_identifier_defaults(mapper, connection, target):
    if getattr(target, "created_at", None) is None:
        target.created_at = datetime.now(timezone.utc)
    if getattr(target, "normalization_failed", None) is None:
        target.normalization_failed = False


@sa_event.listens_for(Reminder, "before_insert")
def _set_reminder_defaults(mapper, connection, target):
    now = datetime.now(timezone.utc)
    if getattr(target, "created_at", None) is None:
        target.created_at = now
    if getattr(target, "updated_at", None) is None:
        target.updated_at = now
    if getattr(target, "enabled", None) is None:
        target.enabled = True
    if getattr(target, "lead_time_days", None) is None:
        target.lead_time_days = 0


@sa_event.listens_for(ReminderOccurrence, "before_insert")
def _set_reminder_occurrence_defaults(mapper, connection, target):
    if getattr(target, "fired_at", None) is None:
        target.fired_at = datetime.now(timezone.utc)
    if getattr(target, "email_attempts", None) is None:
        target.email_attempts = 0


@sa_event.listens_for(PushToken, "before_insert")
def _set_push_token_defaults(mapper, connection, target):
    now = datetime.now(timezone.utc)
    if getattr(target, "created_at", None) is None:
        target.created_at = now
    if getattr(target, "last_used_at", None) is None:
        target.last_used_at = now


@sa_event.listens_for(PendingApproval, "before_insert")
def _set_pending_approval_defaults(mapper, connection, target):
    now = datetime.now(timezone.utc)
    if getattr(target, "created_at", None) is None:
        target.created_at = now
    if getattr(target, "updated_at", None) is None:
        target.updated_at = now
    if getattr(target, "channel", None) is None:
        target.channel = ApprovalChannel.WHATSAPP
    if getattr(target, "state", None) is None:
        target.state = ApprovalState.PENDING_APPROVAL


@sa_event.listens_for(ApprovalHistory, "before_insert")
def _set_approval_history_defaults(mapper, connection, target):
    if getattr(target, "at", None) is None:
        target.at = datetime.now(timezone.utc)


@sa_event.listens_for(ContactPhoto, "before_insert")
def _set_contact_photo_defaults(mapper, connection, target):
    if getattr(target, "uploaded_at", None) is None:
        target.uploaded_at = datetime.now(timezone.utc)


TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"

engine = create_async_engine(
    TEST_DATABASE_URL,
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)


@event.listens_for(engine.sync_engine, "connect")
def _set_sqlite_pragma(dbapi_connection, connection_record):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys = ON")
    cursor.close()


TestSessionLocal = async_sessionmaker(
    bind=engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
)


@pytest_asyncio.fixture(autouse=True)
def reset_rate_limiter():
    """Reset SlowAPI's in-memory counters before each test — all tests share
    the same client IP via the HTTPX test transport, so without this, hit
    counts accumulate across tests and tightly-limited endpoints (login:
    5/minute) start 429-ing tests that run later in the suite."""
    from app.core.rate_limiter import limiter

    limiter._storage.reset()


@pytest_asyncio.fixture(autouse=True)
async def setup_db():
    """Create all tables before each test and drop them after."""
    async with engine.begin() as conn:
        for table in Base.metadata.tables.values():
            for col in table.columns:
                if getattr(col, "server_default", None) is not None:
                    col.server_default = None
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest_asyncio.fixture
async def db_session() -> AsyncGenerator[AsyncSession, None]:
    async with TestSessionLocal() as session:
        yield session


@pytest_asyncio.fixture
async def test_user(db_session: AsyncSession) -> User:
    """A pre-verified user, for tests that need to log in without going
    through the register → verify-email flow first."""
    user = User(
        id=uuid.uuid4(),
        email="testuser@example.com",
        password_hash=hash_password("TestPass123!"),
        email_verified=True,
        failed_login_count=0,
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


@pytest_asyncio.fixture
async def async_client(db_session: AsyncSession) -> AsyncGenerator[AsyncClient, None]:
    """HTTP client bound to the FastAPI app with the DB dependency overridden
    to use the test session — no real token/session wiring bypassed, so auth
    flows exercise the real code path end to end."""
    from app.main import app
    from app.db.session import get_db

    async def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client

    app.dependency_overrides.clear()
