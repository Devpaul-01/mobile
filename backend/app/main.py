"""
main.py — FastAPI application entrypoint.

Error-handling/middleware shape copied from bm-ts/backend/app/main.py.
Routes are mounted under ``/mobile/...`` prefixes (not ``/api``) to keep
mobile and extension API surfaces visually distinct if they're ever proxied
behind the same edge.
"""

import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import Depends, FastAPI, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.routers import approvals, auth, contacts, notifications, reminders, uploads
from app.core.config import settings
from app.core.errors import AppError, ErrorCodes
from app.core.rate_limiter import init_rate_limiter
from app.core.security_headers import SecurityHeadersMiddleware
from app.db.session import get_db
from app.services.scheduler import init_scheduler

logging.basicConfig(level=settings.log_level)

if settings.sentry_dsn:
    import sentry_sdk
    from sentry_sdk.integrations.fastapi import FastApiIntegration
    from sentry_sdk.integrations.sqlalchemy import SqlalchemyIntegration

    sentry_sdk.init(
        dsn=settings.sentry_dsn,
        environment=settings.sentry_environment,
        integrations=[FastApiIntegration(), SqlalchemyIntegration()],
        traces_sample_rate=0.1,
        send_default_pii=False,
    )

openapi_tags = [
    {
        "name": "mobile-auth",
        "description": "Authentication, registration, email verification, and password reset",
    },
    {
        "name": "mobile-contacts",
        "description": "Contact import (with dedup), listing, and management",
    },
    {
        "name": "mobile-reminders",
        "description": "Reminder CRUD and multi-select bulk-apply to several contacts at once",
    },
    {
        "name": "mobile-notifications",
        "description": "Push-token device registration",
    },
    {
        "name": "mobile-approvals",
        "description": "WhatsApp pending-approval queue (approve/report-opened/dismiss)",
    },
    {
        "name": "mobile-uploads",
        "description": "Contact photo upload (presigned Cloudflare R2 upload + confirm)",
    },
]

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Reminder scheduler (Phase 6, §16.2) — off by default in tests (see
    # app/tests/conftest.py setting SCHEDULER_ENABLED=false) so pytest
    # never starts a real 15-minute background job against the per-test
    # SQLite engine, which would outlive the test and touch a dropped DB.
    scheduler = init_scheduler() if settings.scheduler_enabled else None
    try:
        yield
    finally:
        if scheduler is not None:
            scheduler.shutdown(wait=False)


app = FastAPI(
    title="BirthdayMessaging Mobile API",
    version="0.1.0",
    description="Backend API for the BirthdayMessaging mobile app",
    openapi_tags=openapi_tags,
    lifespan=lifespan,
)

init_rate_limiter(app)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "Accept"],
)

app.add_middleware(SecurityHeadersMiddleware)

logger = logging.getLogger(__name__)


def _cors_headers(request: Request) -> dict[str, str]:
    origin = request.headers.get("origin", "")
    if origin in settings.cors_origins:
        return {"access-control-allow-origin": origin, "access-control-allow-credentials": "true"}
    return {}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


_HTTP_STATUS_TO_CODE: dict[int, str] = {
    400: ErrorCodes.BAD_REQUEST,
    401: ErrorCodes.UNAUTHORIZED,
    403: ErrorCodes.FORBIDDEN,
    404: ErrorCodes.NOT_FOUND,
    409: ErrorCodes.CONFLICT,
    422: ErrorCodes.VALIDATION_ERROR,
    429: ErrorCodes.RATE_LIMIT_EXCEEDED,
    500: ErrorCodes.INTERNAL_SERVER_ERROR,
}


@app.exception_handler(AppError)
async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    content: dict = {
        "detail": exc.detail,
        "code": exc.code,
        "timestamp": _now_iso(),
        "errors": ([e.model_dump() for e in exc.errors] if exc.errors else None),
    }
    headers = dict(exc.headers) if exc.headers else {}
    headers.update(_cors_headers(request))
    return JSONResponse(status_code=exc.status_code, content=content, headers=headers or None)


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    field_errors = []
    for error in exc.errors():
        loc = error.get("loc", ())
        field_parts = (
            [str(p) for p in loc[1:]] if len(loc) > 1 else [str(loc[0])] if loc else ["__root__"]
        )
        field_errors.append({"field": ".".join(field_parts), "message": error.get("msg", "")})

    content = {
        "detail": "Validation error",
        "code": ErrorCodes.VALIDATION_ERROR,
        "timestamp": _now_iso(),
        "errors": field_errors,
    }
    return JSONResponse(status_code=422, content=content, headers=_cors_headers(request) or None)


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    code = _HTTP_STATUS_TO_CODE.get(exc.status_code, ErrorCodes.BAD_REQUEST)
    detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
    content = {"detail": detail, "code": code, "timestamp": _now_iso(), "errors": None}
    headers: dict[str, str] = {}
    exc_headers = getattr(exc, "headers", None)
    if exc_headers:
        headers.update(exc_headers)
    headers.update(_cors_headers(request))
    return JSONResponse(status_code=exc.status_code, content=content, headers=headers or None)


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("Unhandled error on %s %s", request.method, request.url.path)
    content = {
        "detail": "Internal server error",
        "code": ErrorCodes.INTERNAL_SERVER_ERROR,
        "timestamp": _now_iso(),
        "errors": None,
    }
    return JSONResponse(status_code=500, content=content, headers=_cors_headers(request) or None)


app.include_router(auth.router, prefix="/mobile/auth", tags=["mobile-auth"])
app.include_router(contacts.router, prefix="/mobile/contacts", tags=["mobile-contacts"])
app.include_router(reminders.router, prefix="/mobile/reminders", tags=["mobile-reminders"])
app.include_router(
    notifications.router, prefix="/mobile/notifications", tags=["mobile-notifications"]
)
app.include_router(approvals.router, prefix="/mobile/approvals", tags=["mobile-approvals"])
app.include_router(uploads.router, prefix="/mobile/uploads", tags=["mobile-uploads"])


@app.get("/health", summary="Service health")
async def health_check(response: Response, db: AsyncSession = Depends(get_db)) -> dict[str, str]:
    try:
        await db.execute(text("SELECT 1"))
    except Exception:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "unavailable", "database": "unavailable"}
    return {"status": "ok", "database": "ok"}
