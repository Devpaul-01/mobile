"""
core/config.py — Application settings loaded from environment variables / .env file.

Pattern copied from bm-ts/backend/app/core/config.py: pydantic_settings.BaseSettings,
fail-fast validators that reject placeholder secrets so the app refuses to start in a
silently-insecure state. New secrets, new codebase — nothing here is shared with the
extension's backend (see MOBILE_APP_ARCHITECTURE_SPEC.md §12.5, §31).

Milestone 1 (auth-only) only requires DATABASE_URL and the JWT_* settings below.
PayPal settings are added in a later phase (§23) — left out entirely for now
rather than stubbed, so an incomplete .env fails loudly at the point a feature
actually needs the missing setting, not silently at startup. SMTP_* (§19, via
plain SMTP rather than the doc's SendGrid framing — team decision) is optional
and degrades to logging instead of sending when unset; see app/services/smtp.py.
Object storage (STORAGE_*, §26) is added in this revision — see the field
comments below for why Cloudflare R2 rather than AWS S3.

REDIS_URL is an exception to the "left out until the phase needs it" rule
above: per an explicit team decision, Redis is adopted now rather than
deferred to Phase 10 as the architecture doc originally scoped it (§25/§29/
§41) — see docs/development/implementation-status.md. It's optional here on
purpose — ``redis_url`` defaults to ``None``, and every Redis-backed feature
(rate limiting in core/rate_limiter.py, session-validity caching in
core/redis_client.py) falls back to in-process/no-op behavior when it's
unset or unreachable, per that same decision (fail open, never fail closed —
a down Redis can only make things slower, never less correct or less secure).
"""

from __future__ import annotations

import json
import logging
from typing import Any

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", case_sensitive=False, extra="ignore"
    )

    database_url: str = Field(
        default="postgresql+asyncpg://postgres:postgres@localhost:5433/bm_mobile",
        alias="DATABASE_URL",
    )

    jwt_secret_key: str = Field(
        default="change-me-super-secret", alias="JWT_SECRET_KEY"
    )
    jwt_algorithm: str = Field(default="HS256", alias="JWT_ALGORITHM")
    access_token_expire_minutes: int = Field(
        default=15, alias="ACCESS_TOKEN_EXPIRE_MINUTES"
    )
    refresh_token_expire_days: int = Field(
        default=30, alias="REFRESH_TOKEN_EXPIRE_DAYS"
    )

    cors_origins: list[str] = Field(
        default_factory=lambda: ["http://localhost:8081"]
    )

    redis_url: str | None = Field(default=None, alias="REDIS_URL")

    # Reminder scheduler (Phase 6, §16.2) — an APScheduler job inside the
    # running app process, not a separate worker (see services/scheduler.py
    # module docstring for the architecture rationale). Defaults on for a
    # real run; the test suite explicitly turns it off (see
    # app/tests/conftest.py) so pytest doesn't start a 15-minute background
    # job against the per-test SQLite engine.
    scheduler_enabled: bool = Field(default=True, alias="SCHEDULER_ENABLED")

    # Object storage (§26) — Cloudflare R2, per an explicit team decision
    # (not AWS S3, despite the architecture doc's generic "S3-compatible
    # storage" framing). R2's API is S3-compatible, so the client code
    # (app/services/storage.py) is a plain boto3 S3 client pointed at a
    # custom endpoint_url — nothing AWS-specific, R2 chosen only via
    # configuration. All four fields are optional so the rest of the app
    # still boots without them (e.g. in CI); app/services/storage.py
    # raises a clear, loud error only when an upload/photo endpoint is
    # actually called without them configured.
    storage_endpoint_url: str | None = Field(default=None, alias="STORAGE_ENDPOINT_URL")
    storage_bucket_name: str | None = Field(default=None, alias="STORAGE_BUCKET_NAME")
    storage_access_key_id: str | None = Field(default=None, alias="STORAGE_ACCESS_KEY_ID")
    storage_secret_access_key: str | None = Field(
        default=None, alias="STORAGE_SECRET_ACCESS_KEY"
    )

    # PayPal (§23/§24, Phase 12) — backend-owned credentials, never sent to
    # the mobile client (only an approval_url and an order id ever reach
    # it; see app/services/paypal.py). Optional, same fail-loud-not-
    # fail-open reasoning as STORAGE_* above: the rest of the app boots
    # fine without these, but any payment endpoint call raises a clear
    # 500 paypal_not_configured rather than silently no-op'ing — there's
    # no sensible degraded mode for "can't actually charge the user."
    # Defaults to PayPal's sandbox API base; set to
    # https://api-m.paypal.com for production.
    paypal_client_id: str | None = Field(default=None, alias="PAYPAL_CLIENT_ID")
    paypal_client_secret: str | None = Field(default=None, alias="PAYPAL_CLIENT_SECRET")
    paypal_webhook_id: str | None = Field(default=None, alias="PAYPAL_WEBHOOK_ID")
    paypal_api_base: str = Field(
        default="https://api-m.sandbox.paypal.com", alias="PAYPAL_API_BASE"
    )

    # Expo push notifications (§16.3/§17.2/§25, Phase 7). Expo's push API
    # (https://exp.host/--/api/v2/push/send) accepts unauthenticated
    # requests, so EXPO_ACCESS_TOKEN is optional — only needed if Expo's
    # "Enhanced Security" per-project access tokens are enabled for this
    # project. See app/services/push.py.
    expo_access_token: str | None = Field(default=None, alias="EXPO_ACCESS_TOKEN")
    expo_push_api_url: str = Field(
        default="https://exp.host/--/api/v2/push/send", alias="EXPO_PUSH_API_URL"
    )

    # SMTP (§19, replaces the architecture doc's SendGrid framing — team
    # decision, see MOBILE_APP_ARCHITECTURE_SPEC.md notes). Used by both
    # auth_tokens.py (verification/reset emails) and email.py (birthday
    # reminder emails) via the shared sender in app/services/smtp.py.
    # Optional, same pattern as STORAGE_*/PAYPAL_*: the app boots fine
    # without these configured (local dev, CI, tests), and the sender
    # logs-and-returns instead of actually dispatching mail in that case.
    # Once SMTP_HOST is set, a send failure is surfaced to the caller
    # (the retry/backoff loop in auth_tokens._deliver / email.py) rather
    # than silently swallowed.
    smtp_host: str | None = Field(default=None, alias="SMTP_HOST")
    smtp_port: int = Field(default=587, alias="SMTP_PORT")
    smtp_username: str | None = Field(default=None, alias="SMTP_USERNAME")
    smtp_password: str | None = Field(default=None, alias="SMTP_PASSWORD")
    smtp_use_tls: bool = Field(default=True, alias="SMTP_USE_TLS")
    smtp_from_email: str = Field(default="noreply@example.com", alias="SMTP_FROM_EMAIL")
    smtp_from_name: str = Field(default="BirthdayMessaging", alias="SMTP_FROM_NAME")

    log_level: str = Field(default="INFO")
    sentry_dsn: str = Field(default="", alias="SENTRY_DSN")
    sentry_environment: str = Field(default="development", alias="SENTRY_ENVIRONMENT")

    @field_validator("jwt_secret_key", mode="after")
    @classmethod
    def validate_jwt_secret(cls, v: str) -> str:
        _INSECURE_DEFAULTS = {
            "change-me-super-secret",
            "secret",
            "changeme",
            "",
            "REPLACE_WITH_GENERATED_SECRET_MIN_32_CHARS",
        }

        if v in _INSECURE_DEFAULTS:
            raise ValueError(
                "JWT_SECRET_KEY cannot be a default placeholder. "
                'Generate a secure random value (>= 32 chars) with: python -c "import secrets; print(secrets.token_hex(32))"'
            )

        if len(v) < 32:
            raise ValueError(
                "JWT_SECRET_KEY is too short (< 32 chars). "
                'Generate a secure random value with: python -c "import secrets; print(secrets.token_hex(32))"'
            )

        return v

    @field_validator("cors_origins", mode="before")
    @classmethod
    def parse_cors(cls, value: Any) -> list[str]:
        # pydantic-settings may pass a pre-parsed list or a raw string
        if isinstance(value, list):
            return [str(o).strip() for o in value if str(o).strip()]
        if isinstance(value, str):
            stripped = value.strip()
            if stripped.startswith("["):
                try:
                    parsed = json.loads(stripped)
                    if isinstance(parsed, list):
                        return [str(o).strip() for o in parsed if str(o).strip()]
                except json.JSONDecodeError:
                    pass
            return [item.strip() for item in stripped.split(",") if item.strip()]
        return list(value)  # type: ignore[arg-type]


settings = Settings()

if settings.jwt_secret_key == "change-me-super-secret" and settings.log_level.upper() in {
    "WARNING",
    "ERROR",
    "CRITICAL",
}:
    logging.getLogger(__name__).warning(
        "JWT_SECRET_KEY looks like a development default but LOG_LEVEL=%s suggests "
        "a production environment.",
        settings.log_level,
    )
