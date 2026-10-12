"""Tests for Redis adoption this phase (session-validity caching, rate
limiter Redis-with-in-memory-fallback) — see core/redis_client.py and
core/rate_limiter.py docstrings for the decision this implements.

Requires a real local Redis for the "actually talks to Redis" cases
(started by whatever runs this suite — see
docs/development/implementation-status.md for the local dev note). The
"Redis unreachable" cases intentionally point at a port nothing listens on,
and must never raise — that's the whole point of the fallback.
"""

from __future__ import annotations

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

_REAL_REDIS_URL = "redis://localhost:6399/0"
_UNREACHABLE_REDIS_URL = "redis://localhost:1/0"  # nothing listens here


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
# Session-validity cache (core/redis_client.py)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_session_cache_round_trip_against_real_redis(monkeypatch):
    monkeypatch.setattr(redis_client.settings, "redis_url", _REAL_REDIS_URL)

    assert await redis_client.is_session_cached_valid("session-a") is None

    await redis_client.cache_session_valid("session-a")
    assert await redis_client.is_session_cached_valid("session-a") is True

    await redis_client.invalidate_session_cache("session-a")
    assert await redis_client.is_session_cached_valid("session-a") is None


@pytest.mark.asyncio
async def test_session_cache_does_not_leak_between_session_ids(monkeypatch):
    monkeypatch.setattr(redis_client.settings, "redis_url", _REAL_REDIS_URL)

    await redis_client.cache_session_valid("session-x")
    assert await redis_client.is_session_cached_valid("session-y") is None
    assert await redis_client.is_session_cached_valid("session-x") is True


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
    import uuid

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


def test_rate_limiter_enforces_limit_against_real_redis():
    client = _build_limited_app(_REAL_REDIS_URL)
    assert client.get("/ping").status_code == 200
    assert client.get("/ping").status_code == 200
    assert client.get("/ping").status_code == 429


def test_rate_limiter_falls_back_to_in_memory_when_redis_unreachable():
    """The whole point of in_memory_fallback_enabled=True: a dead Redis
    must not turn into 500s or an unlimited API — it must keep enforcing
    the limit using an in-process counter instead."""
    client = _build_limited_app(_UNREACHABLE_REDIS_URL)
    assert client.get("/ping").status_code == 200
    assert client.get("/ping").status_code == 200
    assert client.get("/ping").status_code == 429
