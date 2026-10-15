"""
core/security_headers.py — Middleware that injects standard security headers
on every HTTP response.

Adapted from bm-ts/backend/app/core/security_headers.py. The extension
backend's version carves out an inline-script exception for its HTML
password-reset/verify-email pages (see MOBILE_APP_ARCHITECTURE_SPEC.md §22);
this backend's deep-link token endpoints return JSON only, so that exception
is dropped — every response gets the strict CSP.

Headers applied:
  Strict-Transport-Security   — force HTTPS for 1 year (+ subdomains)
  X-Frame-Options             — prevent clickjacking
  X-Content-Type-Options      — disable MIME-type sniffing
  Content-Security-Policy     — restrict resource origins
  Referrer-Policy             — limit referrer leakage
"""

from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        response = await call_next(request)
        response.headers["Strict-Transport-Security"] = (
            "max-age=31536000; includeSubDomains"
        )
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; style-src 'self'"
        )
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = (
            "camera=(), microphone=(), geolocation=()"
        )

        if request.url.path.startswith("/mobile/auth"):
            response.headers["Cache-Control"] = "no-store"
            response.headers["Pragma"] = "no-cache"

        return response
