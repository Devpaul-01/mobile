"""
core/config.py — Application settings loaded from environment variables / .env file.

Pattern copied from bm-ts/backend/app/core/config.py: pydantic_settings.BaseSettings,
fail-fast validators that reject placeholder secrets so the app refuses to start in a
silently-insecure state. New secrets, new codebase — nothing here is shared with the
extension's backend (see MOBILE_APP_ARCHITECTURE_SPEC.md §12.5, §31).

Milestone 1 (auth-only) only requires DATABASE_URL and the JWT_* settings below.
Redis/S3/PayPal/SendGrid settings are added in later phases (§29, §26, §23, §19) —
left out entirely for now rather than stubbed, so an incomplete .env fails loudly at
the point a feature actually needs the missing setting, not silently at startup.
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

    # Reminder scheduler (Phase 6, §16.2) — an APScheduler job inside the
    # running app process, not a separate worker (see services/scheduler.py
    # module docstring for the architecture rationale). Defaults on for a
    # real run; the test suite explicitly turns it off (see
    # app/tests/conftest.py) so pytest doesn't start a 15-minute background
    # job against the per-test SQLite engine.
    scheduler_enabled: bool = Field(default=True, alias="SCHEDULER_ENABLED")

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
