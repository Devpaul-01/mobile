"""Tests for Redis adoption (session-validity caching, rate limiter
Redis-with-in-memory-fallback) — see core/redis_client.py and
core/rate_limiter.py docstrings for the decision this implements.

Tests in this file fall into two groups:
  - "Redis not configured" / "Redis unreachable" cases need no real Redis
    server — the latter intentionally points at a port nothing listens on,
    and must never raise. That's the whole point of the fallback, and it's
    exactly the path every other test in this suite exercises implicitly
    (REDIS_URL is unset in the test environment by default).
  - "Redis is actually up" cases need a real local Redis and are the ones
    CI provides via a service container (see .github/workflows/backend-ci.yml).
    They're skipped automatically, rather than failing, when nothing is
    listening at _REAL_REDIS_URL — see the `_real_redis_available` fixture.
"""

from __future__ import annotations

import socket
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from slowapi.util import get_remote_address
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.core import redis_client

_REAL_REDIS_HOST = "localhost"
_REAL_REDIS_PORT = 6399
_REAL_REDIS_URL = f"redis://{_REAL_REDIS_HOST}:{_REAL_REDIS_PORT}/0"
_UNREACHABLE_REDIS_URL = "redis://localhost:1/0"  # nothing listens here


def _real_redis_is_listening() -> bool:
    try:
        with socket.create_connection((_REAL_REDIS_HOST, _REAL_REDIS_PORT), timeout=0.25):
            return True
    except OSError:
        return False


requires_real_redis = pytest.mark.skipif(
    not _real_redis_is_listening(),
    reason=(
        f"No Redis listening at {_REAL_REDIS_URL} — run one locally "
        "(e.g. `docker run -p 6399:6379 redis:7-alpine`) or rely on CI's "
        "service container (.github/workflows/backend-ci.yml) to exercise "
        "these cases."
    ),
)


@pytest.fixture(autouse=True)
def _reset_redis_client_module_state():
    """``redis_client`` lazily caches its client on module-level globals —
    reset them around every test so each test's ``monkeypatch`` of
    ``settings.redis_url`` actually takes effect for a fresh client."""
    redis_client._client = None
    redis_client._client_init_attempted = False
    yield
    redis_client._client = None
    redis_client._client_init_attempted = False


# ---------------------------------------------------------------------------
# Session-validity cache (core/redis_client.py) — fallback paths, no real
# Redis required.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_session_cache_never_raises_when_redis_unreachable(monkeypatch):
    monkeypatch.setattr(redis_client.settings, "redis_url", _UNREACHABLE_REDIS_URL)

    # None of these should raise — an unreachable Redis must degrade to
    # "always a cache miss," never a request failure.
    await redis_client.cache_session_valid("session-b")
    result = await redis_client.is_session_cached_valid("session-b")
    assert result is None
    await redis_client.invalidate_session_cache("session-b")


@pytest.mark.asyncio
async def test_session_cache_is_inert_when_redis_not_configured(monkeypatch):
    monkeypatch.setattr(redis_client.settings, "redis_url", None)

    await redis_client.cache_session_valid("session-c")
    assert await redis_client.is_session_cached_valid("session-c") is None
    await redis_client.invalidate_session_cache("session-c")  # must not raise


# ---------------------------------------------------------------------------
# Session-validity cache — real Redis required.
# ---------------------------------------------------------------------------


@requires_real_redis
@pytest.mark.asyncio
async def test_session_cache_round_trip_against_real_redis(monkeypatch):
    monkeypatch.setattr(redis_client.settings, "redis_url", _REAL_REDIS_URL)

    assert await redis_client.is_session_cached_valid("session-a") is None

    await redis_client.cache_session_valid("session-a")
    assert await redis_client.is_session_cached_valid("session-a") is True

    await redis_client.invalidate_session_cache("session-a")
    assert await redis_client.is_session_cached_valid("session-a") is None


@requires_real_redis
@pytest.mark.asyncio
async def test_session_cache_does_not_leak_between_session_ids(monkeypatch):
    monkeypatch.setattr(redis_client.settings, "redis_url", _REAL_REDIS_URL)

    await redis_client.cache_session_valid("session-x")
    assert await redis_client.is_session_cached_valid("session-y") is None
    assert await redis_client.is_session_cached_valid("session-x") is True


# ---------------------------------------------------------------------------
# Rate limiter: Redis-backed with in-memory fallback (core/rate_limiter.py)
# ---------------------------------------------------------------------------
#
# These build a small standalone FastAPI app configured exactly the way
# core/rate_limiter.py configures the real one (storage_uri +
# in_memory_fallback_enabled=True), rather than reusing the app-wide
# singleton limiter — that singleton is constructed once at import time
# from whatever REDIS_URL was set in the process environment before the
# test suite started, so it can't be reconfigured per-test.


def _build_limited_app(storage_uri: str | None) -> TestClient:
    # A unique key_prefix per app instance — otherwise a real Redis backend
    # keeps the previous test run's counters (unlike the in-memory
    # fallback's fresh MemoryStorage), causing cross-test/cross-run bleed.
    limiter = Limiter(
        key_func=get_remote_address,
        storage_uri=storage_uri,
        in_memory_fallback_enabled=True,
        key_prefix=f"test-{uuid.uuid4()}",
    )
    app = FastAPI()
    app.state.limiter = limiter

    async def _handler(request, exc):
        return JSONResponse(status_code=429, content={"detail": str(exc.detail)})

    app.add_exception_handler(RateLimitExceeded, _handler)
    app.add_middleware(SlowAPIMiddleware)

    @app.get("/ping")
    @limiter.limit("2/minute")
    async def ping(request: Request):  # noqa: ANN001 - slowapi requires this param name
        return {"ok": True}

    return TestClient(app)


def test_rate_limiter_falls_back_to_in_memory_when_redis_unreachable():
    """The whole point of in_memory_fallback_enabled=True: a dead Redis
    must not turn into 500s or an unlimited API — it must keep enforcing
    the limit using an in-process counter instead. No real Redis needed —
    this is exactly the fallback path."""
    client = _build_limited_app(_UNREACHABLE_REDIS_URL)
    assert client.get("/ping").status_code == 200
    assert client.get("/ping").status_code == 200
    assert client.get("/ping").status_code == 429


def test_rate_limiter_enforces_limit_with_no_storage_uri():
    """Baseline: with no storage_uri at all (REDIS_URL unset, the default
    for this whole test suite), the limiter is pure in-memory and still
    enforces limits — unchanged from before Redis adoption."""
    client = _build_limited_app(None)
    assert client.get("/ping").status_code == 200
    assert client.get("/ping").status_code == 200
    assert client.get("/ping").status_code == 429


@requires_real_redis
def test_rate_limiter_enforces_limit_against_real_redis():
    client = _build_limited_app(_REAL_REDIS_URL)
    assert client.get("/ping").status_code == 200
    assert client.get("/ping").status_code == 200
    assert client.get("/ping").status_code == 429
