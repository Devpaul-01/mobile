"""
core/redis_client.py — Shared async Redis client + session-validity cache.

Adopted per an explicit team decision (see
docs/development/implementation-status.md) rather than deferred to Phase 10
as the architecture doc originally scoped it (§25/§29).

What's cached here, and the deliberate departure from the architecture doc
-----------------------------------------------------------------------
MOBILE_APP_ARCHITECTURE_SPEC.md §25 explicitly says session/token validity
should stay in Postgres and never be cached, to keep revocation durable and
auditable. This module caches session-validity **as a short-TTL positive
cache on top of that Postgres check**, not a replacement for it:

  - A cache hit means "Postgres said this session was valid within the last
    ``_SESSION_CACHE_TTL`` seconds" — it saves a DB round trip on the hot
    path (every authenticated request goes through
    ``api/deps.py get_current_token_payload``), not a correctness decision.
  - A cache miss (including "Redis is unreachable") always falls through to
    the real Postgres check. Nothing is ever treated as valid *because* a
    cache lookup failed.
  - Every place a session is revoked (logout, password-reset-revokes-all —
    see ``api/routers/auth.py _revoke_session_record``) explicitly deletes
    this cache entry, bounding staleness to whichever is smaller: the TTL,
    or the next revocation. A revoked session can appear valid for at most
    ``_SESSION_CACHE_TTL`` seconds if the explicit invalidation call itself
    fails (e.g. Redis down at the moment of revocation) — accepted
    trade-off for the hot-path savings, explicitly flagged rather than
    silently introduced.

Fallback behavior
------------------
Every function here is safe to call whether or not ``REDIS_URL`` is
configured and whether or not Redis is actually reachable right now:
  - If ``settings.redis_url`` is unset, every function is a no-op /
    always-miss — equivalent to Redis not existing at all (this is also
    what every test run does; no test depends on Redis being present).
  - If Redis is configured but a call raises (connection refused, timeout),
    the error is caught and treated as a cache miss/no-op, with a warning
    log — never a request failure. The caller always falls back to hitting
    Postgres directly, which remains correct (if slower).
"""

from __future__ import annotations

import logging
from typing import Optional

from app.core.config import settings

logger = logging.getLogger(__name__)

_SESSION_CACHE_TTL = 60  # seconds — see module docstring for the trade-off this bounds
_SESSION_KEY_PREFIX = "mobile:session_valid:"

_client = None  # type: ignore[var-annotated]
_client_init_attempted = False


def _get_client():
    """Lazily create a single shared async Redis client, or ``None`` if Redis
    isn't configured. Creating the client doesn't connect yet (redis-py is
    lazy) — actual connection errors surface (and are caught) on first use."""
    global _client, _client_init_attempted

    if not settings.redis_url:
        return None

    if _client_init_attempted:
        return _client

    _client_init_attempted = True
    try:
        import redis.asyncio as redis_asyncio

        _client = redis_asyncio.from_url(
            settings.redis_url,
            socket_connect_timeout=1.0,
            socket_timeout=1.0,
            decode_responses=True,
        )
    except Exception as exc:  # pragma: no cover - defensive, e.g. bad URL scheme
        logger.warning("Redis client could not be constructed, disabling it: %s", exc)
        _client = None

    return _client


async def is_session_cached_valid(session_id: str) -> Optional[bool]:
    """Return ``True`` if a recent Postgres check already confirmed this
    session is valid. Returns ``None`` (never ``False``) on a miss or on any
    Redis error — ``None`` means "don't know, check Postgres," which is the
    only safe meaning a cache-unavailable result can have here."""
    client = _get_client()
    if client is None:
        return None
    try:
        value = await client.get(_SESSION_KEY_PREFIX + session_id)
    except Exception as exc:
        logger.warning("Redis session-cache read failed, falling back to DB: %s", exc)
        return None
    return True if value == "1" else None


async def cache_session_valid(session_id: str) -> None:
    """Record that Postgres just confirmed this session is valid, for
    ``_SESSION_CACHE_TTL`` seconds. Best-effort — failure here just means the
    next request pays the DB round trip again, never a correctness issue."""
    client = _get_client()
    if client is None:
        return
    try:
        await client.set(_SESSION_KEY_PREFIX + session_id, "1", ex=_SESSION_CACHE_TTL)
    except Exception as exc:
        logger.warning("Redis session-cache write failed (non-fatal): %s", exc)


async def invalidate_session_cache(session_id: str) -> None:
    """Delete a cached validity entry immediately on revocation (logout,
    password-reset-revokes-all, refresh-reuse-detection). Best-effort: if
    Redis is down right now, the TTL above is the backstop."""
    client = _get_client()
    if client is None:
        return
    try:
        await client.delete(_SESSION_KEY_PREFIX + session_id)
    except Exception as exc:
        logger.warning("Redis session-cache invalidation failed (non-fatal): %s", exc)
