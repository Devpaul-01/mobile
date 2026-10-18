"""Tests for app/core/validators.py — and that sanitize_text is actually
wired into the schemas it was written for, not just defined."""

from __future__ import annotations

import pytest

from app.core.validators import MAX_TEXT_LENGTH, sanitize_text
from app.schemas.auth import UserProfileUpdate
from app.schemas.contact import ContactImportItem, ContactUpdate


def test_sanitize_text_strips_whitespace():
    assert sanitize_text("  Ada  ") == "Ada"


def test_sanitize_text_rejects_null_bytes():
    with pytest.raises(ValueError, match="null byte"):
        sanitize_text("Ada\x00Lovelace")


def test_sanitize_text_rejects_overlong_strings():
    with pytest.raises(ValueError, match="exceed"):
        sanitize_text("a" * (MAX_TEXT_LENGTH + 1))


def test_sanitize_text_passes_through_non_strings():
    # Guard used so this is safe to attach to any Optional[str] field —
    # a None value (field omitted) must not raise.
    assert sanitize_text(None) is None  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Actually wired into schemas — the regression this file exists to catch.
# ---------------------------------------------------------------------------


def test_contact_import_item_name_rejects_null_bytes():
    with pytest.raises(ValueError, match="null byte"):
        ContactImportItem(name="Eve\x00Evil")


def test_contact_update_name_rejects_null_bytes():
    with pytest.raises(ValueError, match="null byte"):
        ContactUpdate(name="Eve\x00Evil")


def test_contact_update_notes_rejects_null_bytes():
    with pytest.raises(ValueError, match="null byte"):
        ContactUpdate(notes="some notes\x00with a null byte")


def test_contact_update_notes_none_is_fine():
    assert ContactUpdate(notes=None).notes is None


def test_user_profile_update_name_rejects_null_bytes():
    with pytest.raises(ValueError, match="null byte"):
        UserProfileUpdate(name="Eve\x00Evil")
