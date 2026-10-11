"""
core/rate_limiter.py — SlowAPI-based rate limiting integration.

Adapted from bm-ts/backend/app/core/rate_limiter.py: that version keys on a
Facebook user ID (set on request.state by the extension's auth flow), which
has no mobile equivalent. This keys on remote IP only, which is all SlowAPI's
in-process limiter needs for the unauthenticated endpoints it guards here
(register/login/password-reset) — the same endpoints the extension backend
rate-limits this way today.

This is in-process and per-worker, same limitation the architecture doc notes
for the extension backend (MOBILE_APP_ARCHITECTURE_SPEC.md §3, §25): it does
not share state across gunicorn workers. The per-user-id limits in §29
(contact import, reminder create, general API) need the shared state a
multi-worker deployment requires and are implemented on Redis in Phase 10,
not here — this module only covers the unauthenticated, IP-keyed endpoints
that exist in Milestone 1.
"""

from slowapi import Limiter
from slowapi.middleware import SlowAPIMiddleware
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
from starlette.requests import Request
from starlette.responses import JSONResponse

limiter = Limiter(key_func=get_remote_address)


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
