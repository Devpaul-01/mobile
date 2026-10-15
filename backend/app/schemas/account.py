"""
schemas/account.py — Pydantic schemas for GDPR export / account deletion
(MOBILE_APP_ARCHITECTURE_SPEC.md §27, Phase 13).

The export response (§27.4) is intentionally not a single rigid
``BaseModel`` — it's a streamed JSON document covering every owned
entity type (§27.1), and new entity types get added here as the schema
grows. ``app/services/account.py export_user_data`` returns a plain
``dict`` built from the same field names these schemas describe, so this
file exists to document the export's shape, not to validate it at the
response boundary.
"""

from __future__ import annotations

from pydantic import BaseModel


class DeleteAccountRequest(BaseModel):
    password: str
