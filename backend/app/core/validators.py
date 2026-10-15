"""
core/validators.py — Reusable Pydantic field validators for input sanitization.

Copied verbatim from bm-ts/backend/app/core/validators.py.

Usage in any schema::

    from app.core.validators import sanitize_text

    class MySchema(BaseModel):
        name: str

        _sanitize = field_validator("name", mode="after")(sanitize_text)
"""

from __future__ import annotations

MAX_TEXT_LENGTH = 10_000


def sanitize_text(v: str) -> str:
    """Strip whitespace and reject null bytes / excessively long strings."""
    if not isinstance(v, str):
        return v
    v = v.strip()
    if "\x00" in v:
        raise ValueError("Text must not contain null bytes")
    if len(v) > MAX_TEXT_LENGTH:
        raise ValueError(f"Text must not exceed {MAX_TEXT_LENGTH:,} characters")
    return v
