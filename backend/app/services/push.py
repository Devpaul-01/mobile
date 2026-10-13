"""
services/push.py — Push notification sending.

Stubbed — logs instead of calling Expo's push API (architecture doc §25,
Phase 7). No mobile client exists yet to register a real device token
against, so there's nothing to actually call. Push is fire-and-forget by
design (§16.3 — failures are never retried), which is why, unlike email,
this has no return value the caller needs to act on.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


async def send_push_notification(*, device_token: str, contact_name: str) -> None:
    logger.info(
        "[dev push stub] birthday reminder for %s, to device token %s",
        contact_name,
        device_token,
    )


async def send_approval_queue_push(*, device_token: str, contact_name: str) -> None:
    """A distinct notification from ``send_push_notification`` above — this
    one tells the user something is waiting in their Approval Queue
    (architecture doc §17.2: creating a ``PendingApproval`` "also triggers
    a push notification pointing at the Approval Queue screen"), sent
    regardless of whether ``push`` is also one of the reminder's own
    channels. Still a logging stub, same as every other send in this
    module today."""
    logger.info(
        "[dev push stub] WhatsApp approval waiting for %s, to device token %s",
        contact_name,
        device_token,
    )
