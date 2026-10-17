"""
services/push.py — Push notification sending via Expo's push API.

Calls https://exp.host/--/api/v2/push/send (architecture doc §16.3/§17.2/
§25, Phase 7). Push is fire-and-forget by design (§16.3 — failures are
never retried), which is why, unlike email, these functions have no
return value the caller needs to act on: a failed or malformed
device_token logs a warning and is otherwise swallowed here, matching the
"best-effort" framing already on PushToken's model docstring.

Deliberately does not attempt to deregister/prune PushToken rows on an
Expo "DeviceNotRegistered" receipt — that would need a DB session passed
into this module, which doesn't exist in its current fire-and-forget
signature. A natural follow-up once that plumbing exists, not done here.
"""

from __future__ import annotations

import logging

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)

_REQUEST_TIMEOUT = 10.0


async def _send(*, device_token: str, title: str, body: str) -> None:
    if not device_token.startswith(("ExponentPushToken[", "ExpoPushToken[")):
        # Not a real Expo push token — e.g. test fixtures, or a client bug.
        # Fire-and-forget means we log and move on rather than raising.
        logger.warning(
            "Skipping push send: %r doesn't look like an Expo push token",
            device_token,
        )
        return

    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    if settings.expo_access_token:
        headers["Authorization"] = f"Bearer {settings.expo_access_token}"

    payload = {
        "to": device_token,
        "title": title,
        "body": body,
        "sound": "default",
    }

    try:
        async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT) as client:
            response = await client.post(
                settings.expo_push_api_url, json=payload, headers=headers
            )
        response.raise_for_status()
        data = response.json()
        ticket = (data.get("data") or {}) if isinstance(data, dict) else {}
        if isinstance(ticket, dict) and ticket.get("status") == "error":
            logger.warning(
                "Expo push ticket error for %s: %s",
                device_token,
                ticket.get("message") or ticket.get("details"),
            )
    except Exception:
        logger.exception("Failed to send push notification to %s", device_token)


async def send_push_notification(*, device_token: str, contact_name: str) -> None:
    await _send(
        device_token=device_token,
        title="Birthday reminder",
        body=f"It's {contact_name}'s birthday coming up.",
    )


async def send_approval_queue_push(*, device_token: str, contact_name: str) -> None:
    """A distinct notification from ``send_push_notification`` above — this
    one tells the user something is waiting in their Approval Queue
    (architecture doc §17.2: creating a ``PendingApproval`` "also triggers
    a push notification pointing at the Approval Queue screen"), sent
    regardless of whether ``push`` is also one of the reminder's own
    channels."""
    await _send(
        device_token=device_token,
        title="Approval needed",
        body=f"A WhatsApp message for {contact_name} is waiting for your approval.",
    )
