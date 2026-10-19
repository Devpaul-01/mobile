"""
core/rate_limiter.py — SlowAPI-based rate limiting integration.

Adapted from bm-ts/backend/app/core/rate_limiter.py: that version keys on a
Facebook user ID (set on request.state by the extension's auth flow), which
has no mobile equivalent. This keys on remote IP only, which is all SlowAPI's
in-process limiter needs for the unauthenticated endpoints it guards here
(register/login/password-reset) — the same endpoints the extension backend
rate-limits this way today.

Redis-backed, adopted per an explicit team decision
-----------------------------------------------------
Per an explicit team decision (see
docs/development/implementation-status.md), this is Redis-backed when
``REDIS_URL`` is configured — not deferred to Phase 10 as the architecture
doc originally scoped it (§25/§29) — so limits are actually shared across
multiple backend instances/workers, which an in-process counter cannot do.

Fallback to in-memory when Redis is down
------------------------------------------
``in_memory_fallback_enabled=True`` is SlowAPI/``limits``' own built-in
behavior for exactly this: if a call to Redis raises, the limiter marks its
Redis storage dead, switches to an in-process ``MemoryStorage`` for
subsequent checks, and retries the real storage on a later call (so it
recovers automatically once Redis comes back) — nothing custom needed here.
This matches the team's explicit decision to fail open to in-memory limiting
(never fail closed/503) when Redis is unreachable, for every limit in this
module. If ``REDIS_URL`` isn't set at all (e.g. local dev, or the test
suite), the limiter is pure in-memory from the start, same as before this
change.

Deliberately not using SlowAPI's ``swallow_errors`` option here — that
swallows *any* exception raised while checking/enforcing a limit, not just
storage-connectivity ones, which would silently let requests through on a
real bug rather than a Redis outage specifically. ``in_memory_fallback``
already covers the actual "Redis is down" case precisely.

The per-user-id limits in §29 (contact import, reminder create, general
API) still need per-route work beyond what this module does today — this
module makes the *storage* shared/multi-worker-safe, but the existing
limits here are still IP-keyed, matching what's actually enforced on every
route today (none of Milestone 1-8's routes key on user id yet).
"""

from slowapi import Limiter
from slowapi.middleware import SlowAPIMiddleware
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.core.config import settings

limiter = Limiter(
    key_func=get_remote_address,
    storage_uri=settings.redis_url,
    in_memory_fallback_enabled=True,
)


async def _json_rate_limit_exceeded_handler(
    request: Request, exc: RateLimitExceeded
) -> JSONResponse:
    """Return a JSON 429 response so mobile clients never see plain text."""
    return JSONResponse(
        status_code=429,
        content={"detail": f"Rate limit exceeded: {exc.detail}"},
    )


def init_rate_limiter(app):
    """Attach SlowAPI middleware and the 429 exception handler to the FastAPI application."""
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _json_rate_limit_exceeded_handler)
    app.add_middleware(SlowAPIMiddleware)
