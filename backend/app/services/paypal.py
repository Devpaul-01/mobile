"""
services/paypal.py — PayPal integration (MOBILE_APP_ARCHITECTURE_SPEC.md
§23/§24, Phase 12).

Implements §23's diagrammed flow literally: the PayPal **Orders** API
(create-order + capture), not the separate PayPal *Subscriptions* API —
§23's own diagram and §35's API inventory both name exactly
create-order/capture/webhook, never a create-subscription endpoint. See
``Subscription``'s docstring in app/db/models.py for the full reasoning.
"Renewal" under this model means the user re-initiating create-order/
capture before their ``current_period_end`` lapses — there is no PayPal-
driven automatic recurring charge here, which is a deliberate, flagged
scope decision following the doc's literal wording rather than silently
building a different (Subscriptions-API) integration than what's
specified.

Security boundary (§32's "never trust the client" table, applied here):
this module is the *only* thing that ever talks to PayPal with real
credentials. The mobile app never sees ``PAYPAL_CLIENT_SECRET`` — only an
``approval_url`` to open and an ``order_id`` to hand back. Whether an
order was actually approved and paid for is something only PayPal's own
API can confirm; nothing here takes the client's word for it.

Mocking-friendly design, matching this codebase's established test style
(monkeypatch at the function boundary — see test_uploads.py, test_scheduler.py):
all actual network I/O goes through the single ``_request`` function, so
tests monkeypatch ``app.services.paypal._request`` rather than mocking
httpx internals or pulling in a PayPal sandbox dependency.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import httpx
from fastapi import status

from app.core.config import settings
from app.core.errors import AppError, ErrorCodes

# Placeholder pricing table — §29-style explicit caveat: these are
# starting values for MVP, not a settled pricing decision; the
# architecture doc specifies no concrete prices anywhere, so these are
# this milestone's own necessary concretization and need real business
# sign-off before launch.
PLAN_CATALOG: dict[str, dict[str, Any]] = {
    "monthly": {"amount": Decimal("4.99"), "currency": "USD", "duration_days": 30},
    "yearly": {"amount": Decimal("39.99"), "currency": "USD", "duration_days": 365},
}

_ORDER_RETURN_PATH = "mobile/payments/paypal/return"  # Universal Link path, §22/§23


@dataclass
class CreatedOrder:
    order_id: str
    approval_url: str


@dataclass
class CapturedOrder:
    status: str  # PayPal's own capture status string, e.g. "COMPLETED"
    amount: Decimal
    currency: str


def validate_plan(plan: str) -> dict[str, Any]:
    if plan not in PLAN_CATALOG:
        raise AppError(
            status.HTTP_400_BAD_REQUEST,
            ErrorCodes.INVALID_PLAN,
            f"Unknown plan '{plan}'. Available: {', '.join(sorted(PLAN_CATALOG))}",
        )
    return PLAN_CATALOG[plan]


def _raise_not_configured() -> None:
    raise AppError(
        status.HTTP_500_INTERNAL_SERVER_ERROR,
        ErrorCodes.PAYPAL_NOT_CONFIGURED,
        "PayPal is not configured on this server.",
    )


def _require_configured() -> None:
    if not (settings.paypal_client_id and settings.paypal_client_secret):
        _raise_not_configured()


async def _request(
    method: str,
    path: str,
    *,
    json: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    """The single network-call chokepoint for this module — tests
    monkeypatch this function directly rather than mocking httpx."""
    async with httpx.AsyncClient(base_url=settings.paypal_api_base, timeout=15.0) as client:
        return await client.request(method, path, json=json, headers=headers)


async def _get_access_token() -> str:
    """OAuth2 client-credentials grant (PayPal's standard server-to-server
    auth) — fetched fresh per call rather than cached, since order volume
    at this project's scale doesn't justify the added complexity of
    tracking the token's own expiry; a straightforward optimization if
    volume ever warrants it."""
    _require_configured()
    basic = base64.b64encode(
        f"{settings.paypal_client_id}:{settings.paypal_client_secret}".encode()
    ).decode()
    response = await _request(
        "POST",
        "/v1/oauth2/token",
        headers={
            "Authorization": f"Basic {basic}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        json=None,
    )
    if response.status_code != 200:
        raise AppError(
            status.HTTP_502_BAD_GATEWAY,
            ErrorCodes.PAYPAL_ERROR,
            "Failed to authenticate with PayPal.",
        )
    return response.json()["access_token"]


async def create_order(*, plan: str) -> CreatedOrder:
    """Creates a PayPal order for the given plan and returns the order id
    plus the hosted approval URL the mobile app opens in an in-app
    browser (§23 step 1-2)."""
    plan_info = validate_plan(plan)
    token = await _get_access_token()

    response = await _request(
        "POST",
        "/v2/checkout/orders",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "intent": "CAPTURE",
            "purchase_units": [
                {
                    "amount": {
                        "currency_code": plan_info["currency"],
                        "value": str(plan_info["amount"]),
                    },
                    "description": f"BirthdayMessaging {plan} subscription",
                }
            ],
            "application_context": {
                "return_url": f"{_ORDER_RETURN_PATH}?result=approved",
                "cancel_url": f"{_ORDER_RETURN_PATH}?result=cancelled",
            },
        },
    )
    if response.status_code not in (200, 201):
        raise AppError(
            status.HTTP_502_BAD_GATEWAY,
            ErrorCodes.PAYPAL_ERROR,
            "PayPal rejected the order creation request.",
        )
    body = response.json()
    approval_url = next(
        (link["href"] for link in body.get("links", []) if link.get("rel") == "approve"),
        "",
    )
    return CreatedOrder(order_id=body["id"], approval_url=approval_url)


async def capture_order(order_id: str) -> CapturedOrder:
    """Server-to-server capture — the actual verification step (§23's
    explicit "the mobile app's claim that 'the user approved' is NEVER
    trusted on its own"). PayPal itself rejects capturing an order that
    was never approved, so a client can't forge success by calling this
    with a fabricated or not-yet-approved order id."""
    token = await _get_access_token()

    response = await _request(
        "POST",
        f"/v2/checkout/orders/{order_id}/capture",
        headers={"Authorization": f"Bearer {token}"},
        json={},
    )
    if response.status_code not in (200, 201):
        raise AppError(
            status.HTTP_502_BAD_GATEWAY,
            ErrorCodes.PAYPAL_ERROR,
            "PayPal could not capture this order — it may not have been approved.",
        )
    body = response.json()
    capture = body["purchase_units"][0]["payments"]["captures"][0]
    return CapturedOrder(
        status=capture["status"],
        amount=Decimal(capture["amount"]["value"]),
        currency=capture["amount"]["currency_code"],
    )


async def verify_webhook_signature(*, headers: dict[str, str], body: bytes) -> bool:
    """Calls PayPal's own verify-webhook-signature endpoint — the
    recommended server-side verification method (rather than reimplementing
    PayPal's certificate-chain validation locally). Returns ``False`` for
    any signature PayPal doesn't confirm as ``SUCCESS``, including when
    PAYPAL_WEBHOOK_ID isn't configured (handled below, not by
    _require_configured, since an unconfigured-but-reachable webhook
    endpoint should reject cleanly rather than 500 — a webhook is called
    by PayPal, not the mobile client, so there's no one for a loud 500 to
    usefully inform)."""
    if not (settings.paypal_client_id and settings.paypal_client_secret and settings.paypal_webhook_id):
        return False

    import json as json_module

    token = await _get_access_token()
    event_body = json_module.loads(body)
    verification_payload = {
        "transmission_id": headers.get("paypal-transmission-id", ""),
        "transmission_time": headers.get("paypal-transmission-time", ""),
        "cert_url": headers.get("paypal-cert-url", ""),
        "auth_algo": headers.get("paypal-auth-algo", ""),
        "transmission_sig": headers.get("paypal-transmission-sig", ""),
        "webhook_id": settings.paypal_webhook_id,
        "webhook_event": event_body,
    }
    response = await _request(
        "POST",
        "/v1/notifications/verify-webhook-signature",
        headers={"Authorization": f"Bearer {token}"},
        json=verification_payload,
    )
    if response.status_code != 200:
        return False
    return response.json().get("verification_status") == "SUCCESS"


def compute_period_end(plan: str) -> datetime:
    plan_info = validate_plan(plan)
    return datetime.now(timezone.utc) + timedelta(days=plan_info["duration_days"])
