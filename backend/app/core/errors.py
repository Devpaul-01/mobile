"""
core/errors.py — Structured application error infrastructure.

Shape copied from bm-ts/backend/app/core/errors.py. Error codes below are
scoped to what this milestone's routers actually raise (auth only) — extend
this list as later milestones add contacts/reminders/approvals/payments
rather than pre-declaring codes nothing raises yet.

Usage in routers
----------------
    from app.core.errors import AppError, ErrorCodes

    raise AppError(status.HTTP_404_NOT_FOUND, ErrorCodes.NOT_FOUND, "Not found")
"""

from __future__ import annotations

from typing import Any, Optional, NoReturn

from app.schemas.common import FieldError


class ErrorCodes:
    # Authentication / session
    INVALID_CREDENTIALS = "invalid_credentials"
    INVALID_REFRESH_TOKEN = "invalid_refresh_token"
    EMAIL_ALREADY_REGISTERED = "email_already_registered"
    EMAIL_NOT_VERIFIED = "email_not_verified"
    ACCOUNT_LOCKED = "account_locked"
    SESSION_NOT_FOUND = "session_not_found"

    # Generic resource errors
    NOT_FOUND = "not_found"
    CONFLICT = "conflict"

    # System
    RATE_LIMIT_EXCEEDED = "rate_limit_exceeded"
    VALIDATION_ERROR = "validation_error"
    INTERNAL_SERVER_ERROR = "internal_server_error"
    UNAUTHORIZED = "unauthorized"
    FORBIDDEN = "forbidden"
    BAD_REQUEST = "bad_request"


class AppError(Exception):
    """Raise instead of HTTPException to produce a structured error response.

    Parameters
    ----------
    status_code:
        HTTP status code (use ``fastapi.status`` constants).
    code:
        Machine-readable snake_case error code from ``ErrorCodes``.
    detail:
        Human-readable explanation for this specific occurrence.
    errors:
        Optional list of per-field validation errors.
    headers:
        Optional HTTP headers to include in the response (e.g.
        ``{"WWW-Authenticate": "Bearer"}`` for 401 responses).
    """

    def __init__(
        self,
        status_code: int,
        code: str,
        detail: str,
        *,
        errors: Optional[list[FieldError]] = None,
        headers: Optional[dict[str, Any]] = None,
    ) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.code = code
        self.detail = detail
        self.errors = errors
        self.headers = headers


def raise_app_error(
    status_code: int,
    code: str,
    detail: str,
    *,
    errors: Optional[list[FieldError]] = None,
    headers: Optional[dict[str, Any]] = None,
) -> NoReturn:
    """Convenience one-liner that constructs and raises an ``AppError``."""
    raise AppError(status_code, code, detail, errors=errors, headers=headers)
