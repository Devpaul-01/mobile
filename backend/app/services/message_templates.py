"""
services/message_templates.py — Fixed birthday-message templates.

One template per channel, server-side, parameterized by contact first
name only — deliberately not a user-configurable template *editor* in
this pass (matches the same restraint the architecture doc applies to
WhatsApp's own message in §20.3: "a short pre-filled message... not a
full rich-template system in MVP"). Plain string formatting is enough at
this content size; no templating engine needed.

Used to seed ``PendingApproval.message_subject``/``message_body`` once,
at creation time (services/scheduler.py) — the user's own edits
afterward (``PATCH /mobile/approvals/{id}``) are never overwritten by a
later scheduler pass touching the same row.

WhatsApp's message is still composed client-side at tap time (§20.3,
unchanged) — ``whatsapp_message`` here exists only so the email and
whatsapp templates share one obvious home and stay in sync in tone,
should the client ever want to mirror this copy; it is not currently
called by any email or whatsapp send path for the whatsapp channel.
"""

from __future__ import annotations


def _first_name(contact_name: str) -> str:
    return contact_name.strip().split(" ")[0] if contact_name.strip() else contact_name


def email_subject(contact_name: str) -> str:
    return f"Happy Birthday, {_first_name(contact_name)}! 🎉"


def email_body(contact_name: str) -> str:
    name = _first_name(contact_name)
    return (
        f"Hi {name},\n\n"
        f"Wishing you a very happy birthday! Hope your day is filled with "
        f"everything you love.\n\n"
        f"Best,\n"
    )


def whatsapp_message(contact_name: str) -> str:
    return f"Happy birthday, {_first_name(contact_name)}! 🎉"
