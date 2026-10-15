"""Tests for PayPal payments (§23/§24) — app/services/paypal.py,
app/services/payments.py, app/api/routers/payments.py.

Matches this codebase's established mocking style (monkeypatch at the
function boundary — see test_uploads.py, test_scheduler.py): all real
network I/O in app/services/paypal.py funnels through the single
``_request`` function, so these tests monkeypatch that one function with
a small fake PayPal server rather than mocking httpx internals or hitting
PayPal's real sandbox."""

from __future__ import annotations

import json as json_module

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.security import hash_password
from app.db.models import User

pytestmark = pytest.mark.asyncio


class _FakeResponse:
    def __init__(self, status_code: int, data: dict):
        self.status_code = status_code
        self._data = data

    def json(self) -> dict:
        return self._data


class _FakePayPal:
    def __init__(self):
        self._order_counter = 0
        self.capture_should_fail: set[str] = set()
        self.capture_overrides: dict[str, dict] = {}
        self.webhook_verifies = True
        self.created_orders: dict[str, dict] = {}

    def new_order_id(self) -> str:
        self._order_counter += 1
        return f"ORDER{self._order_counter}"


def _install_fake_paypal(monkeypatch, fake: _FakePayPal):
    async def _fake_request(method, path, *, json=None, headers=None):
        if path == "/v1/oauth2/token":
            return _FakeResponse(200, {"access_token": "fake-access-token"})

        if method == "POST" and path == "/v2/checkout/orders":
            order_id = fake.new_order_id()
            unit = json["purchase_units"][0]["amount"]
            fake.created_orders[order_id] = {
                "amount": unit["value"],
                "currency": unit["currency_code"],
            }
            return _FakeResponse(
                201,
                {
                    "id": order_id,
                    "links": [
                        {
                            "rel": "approve",
                            "href": f"https://fake-paypal.example.com/approve/{order_id}",
                        }
                    ],
                },
            )

        if method == "POST" and path.startswith("/v2/checkout/orders/") and path.endswith(
            "/capture"
        ):
            order_id = path.split("/")[4]
            if order_id in fake.capture_should_fail:
                return _FakeResponse(422, {"error": "ORDER_NOT_APPROVED"})
            override = fake.capture_overrides.get(order_id, {})
            created = fake.created_orders.get(order_id, {"amount": "4.99", "currency": "USD"})
            amount = override.get("amount", created["amount"])
            currency = override.get("currency", created["currency"])
            return _FakeResponse(
                201,
                {
                    "purchase_units": [
                        {
                            "payments": {
                                "captures": [
                                    {
                                        "status": "COMPLETED",
                                        "amount": {"value": amount, "currency_code": currency},
                                    }
                                ]
                            }
                        }
                    ]
                },
            )

        if method == "POST" and path == "/v1/notifications/verify-webhook-signature":
            return _FakeResponse(
                200,
                {"verification_status": "SUCCESS" if fake.webhook_verifies else "FAILURE"},
            )

        raise AssertionError(f"unexpected PayPal call: {method} {path}")

    monkeypatch.setattr("app.services.paypal._request", _fake_request)
    monkeypatch.setattr(settings, "paypal_client_id", "fake-client-id")
    monkeypatch.setattr(settings, "paypal_client_secret", "fake-client-secret")
    monkeypatch.setattr(settings, "paypal_webhook_id", "fake-webhook-id")


@pytest.fixture
def fake_paypal(monkeypatch):
    fake = _FakePayPal()
    _install_fake_paypal(monkeypatch, fake)
    return fake


async def _auth_headers(async_client: AsyncClient, db_session: AsyncSession, email: str) -> dict:
    user = User(
        email=email,
        password_hash=hash_password("TestPass123!"),
        email_verified=True,
        failed_login_count=0,
    )
    db_session.add(user)
    await db_session.commit()

    login_resp = await async_client.post(
        "/mobile/auth/login", json={"email": email, "password": "TestPass123!"}
    )
    assert login_resp.status_code == 200
    return {"Authorization": f"Bearer {login_resp.json()['access_token']}"}


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------


async def test_status_none_when_never_subscribed(
    async_client: AsyncClient, db_session: AsyncSession, fake_paypal
):
    headers = await _auth_headers(async_client, db_session, "nosub@example.com")
    resp = await async_client.get("/mobile/payments/status", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["status"] == "none"


# ---------------------------------------------------------------------------
# Create order
# ---------------------------------------------------------------------------


async def test_create_order_requires_authentication(async_client: AsyncClient, fake_paypal):
    resp = await async_client.post(
        "/mobile/payments/paypal/create-order", json={"plan": "monthly"}
    )
    assert resp.status_code == 401


async def test_create_order_rejects_unknown_plan(
    async_client: AsyncClient, db_session: AsyncSession, fake_paypal
):
    headers = await _auth_headers(async_client, db_session, "badplan@example.com")
    resp = await async_client.post(
        "/mobile/payments/paypal/create-order", json={"plan": "lifetime"}, headers=headers
    )
    assert resp.status_code == 400
    assert resp.json()["code"] == "invalid_plan"


async def test_create_order_returns_approval_url(
    async_client: AsyncClient, db_session: AsyncSession, fake_paypal
):
    headers = await _auth_headers(async_client, db_session, "createorder@example.com")
    resp = await async_client.post(
        "/mobile/payments/paypal/create-order", json={"plan": "monthly"}, headers=headers
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["paypal_order_id"]
    assert body["approval_url"].startswith("https://fake-paypal.example.com/approve/")

    status_resp = await async_client.get("/mobile/payments/status", headers=headers)
    assert status_resp.json()["status"] == "pending"
    assert status_resp.json()["plan"] == "monthly"


async def test_create_order_rejects_when_already_active(
    async_client: AsyncClient, db_session: AsyncSession, fake_paypal
):
    headers = await _auth_headers(async_client, db_session, "alreadysub@example.com")

    create_resp = await async_client.post(
        "/mobile/payments/paypal/create-order", json={"plan": "monthly"}, headers=headers
    )
    order_id = create_resp.json()["paypal_order_id"]
    capture_resp = await async_client.post(
        "/mobile/payments/paypal/capture", json={"paypal_order_id": order_id}, headers=headers
    )
    assert capture_resp.status_code == 200

    second_resp = await async_client.post(
        "/mobile/payments/paypal/create-order", json={"plan": "yearly"}, headers=headers
    )
    assert second_resp.status_code == 400
    assert second_resp.json()["code"] == "already_subscribed"


# ---------------------------------------------------------------------------
# Capture
# ---------------------------------------------------------------------------


async def test_capture_rejects_unknown_order(
    async_client: AsyncClient, db_session: AsyncSession, fake_paypal
):
    headers = await _auth_headers(async_client, db_session, "unknownorder@example.com")
    resp = await async_client.post(
        "/mobile/payments/paypal/capture",
        json={"paypal_order_id": "NEVER-CREATED"},
        headers=headers,
    )
    assert resp.status_code == 400
    assert resp.json()["code"] == "order_not_found"


async def test_capture_rejects_order_belonging_to_another_user(
    async_client: AsyncClient, db_session: AsyncSession, fake_paypal
):
    owner_headers = await _auth_headers(async_client, db_session, "owner@example.com")
    create_resp = await async_client.post(
        "/mobile/payments/paypal/create-order", json={"plan": "monthly"}, headers=owner_headers
    )
    order_id = create_resp.json()["paypal_order_id"]

    other_headers = await _auth_headers(async_client, db_session, "intruder@example.com")
    resp = await async_client.post(
        "/mobile/payments/paypal/capture",
        json={"paypal_order_id": order_id},
        headers=other_headers,
    )
    assert resp.status_code == 400
    assert resp.json()["code"] == "order_mismatch"


async def test_capture_activates_subscription(
    async_client: AsyncClient, db_session: AsyncSession, fake_paypal
):
    headers = await _auth_headers(async_client, db_session, "capturesuccess@example.com")
    create_resp = await async_client.post(
        "/mobile/payments/paypal/create-order", json={"plan": "yearly"}, headers=headers
    )
    order_id = create_resp.json()["paypal_order_id"]

    resp = await async_client.post(
        "/mobile/payments/paypal/capture", json={"paypal_order_id": order_id}, headers=headers
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "active"
    assert body["plan"] == "yearly"

    status_resp = await async_client.get("/mobile/payments/status", headers=headers)
    status_body = status_resp.json()
    assert status_body["status"] == "active"
    assert status_body["current_period_end"] is not None
    assert status_body["cancel_at_period_end"] is False


async def test_capture_is_idempotent_on_order_id(
    async_client: AsyncClient, db_session: AsyncSession, fake_paypal
):
    headers = await _auth_headers(async_client, db_session, "idempotent@example.com")
    create_resp = await async_client.post(
        "/mobile/payments/paypal/create-order", json={"plan": "monthly"}, headers=headers
    )
    order_id = create_resp.json()["paypal_order_id"]

    first = await async_client.post(
        "/mobile/payments/paypal/capture", json={"paypal_order_id": order_id}, headers=headers
    )
    second = await async_client.post(
        "/mobile/payments/paypal/capture", json={"paypal_order_id": order_id}, headers=headers
    )
    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json() == first.json()


async def test_capture_failure_marks_payment_failed_and_returns_502(
    async_client: AsyncClient, db_session: AsyncSession, fake_paypal
):
    headers = await _auth_headers(async_client, db_session, "capturefails@example.com")
    create_resp = await async_client.post(
        "/mobile/payments/paypal/create-order", json={"plan": "monthly"}, headers=headers
    )
    order_id = create_resp.json()["paypal_order_id"]
    fake_paypal.capture_should_fail.add(order_id)

    resp = await async_client.post(
        "/mobile/payments/paypal/capture", json={"paypal_order_id": order_id}, headers=headers
    )
    assert resp.status_code == 502
    assert resp.json()["code"] == "paypal_error"

    status_resp = await async_client.get("/mobile/payments/status", headers=headers)
    assert status_resp.json()["status"] == "pending"  # never activated


# ---------------------------------------------------------------------------
# Webhook
# ---------------------------------------------------------------------------


def _webhook_event(event_type: str, order_id: str) -> bytes:
    return json_module.dumps(
        {
            "event_type": event_type,
            "resource": {
                "id": "CAPTURE-ID-IGNORED",
                "supplementary_data": {"related_ids": {"order_id": order_id}},
            },
        }
    ).encode()


async def test_webhook_rejects_invalid_signature(
    async_client: AsyncClient, db_session: AsyncSession, fake_paypal
):
    fake_paypal.webhook_verifies = False
    resp = await async_client.post(
        "/mobile/payments/paypal/webhook",
        content=_webhook_event("PAYMENT.CAPTURE.COMPLETED", "WHATEVER"),
        headers={"content-type": "application/json"},
    )
    assert resp.status_code == 400
    assert resp.json()["code"] == "invalid_webhook_signature"


async def test_webhook_activates_subscription_on_capture_completed(
    async_client: AsyncClient, db_session: AsyncSession, fake_paypal
):
    headers = await _auth_headers(async_client, db_session, "webhookuser@example.com")
    create_resp = await async_client.post(
        "/mobile/payments/paypal/create-order", json={"plan": "monthly"}, headers=headers
    )
    order_id = create_resp.json()["paypal_order_id"]

    resp = await async_client.post(
        "/mobile/payments/paypal/webhook",
        content=_webhook_event("PAYMENT.CAPTURE.COMPLETED", order_id),
        headers={"content-type": "application/json"},
    )
    assert resp.status_code == 200

    status_resp = await async_client.get("/mobile/payments/status", headers=headers)
    assert status_resp.json()["status"] == "active"


async def test_webhook_cancels_subscription_on_capture_denied(
    async_client: AsyncClient, db_session: AsyncSession, fake_paypal
):
    headers = await _auth_headers(async_client, db_session, "webhookdenied@example.com")
    create_resp = await async_client.post(
        "/mobile/payments/paypal/create-order", json={"plan": "monthly"}, headers=headers
    )
    order_id = create_resp.json()["paypal_order_id"]
    await async_client.post(
        "/mobile/payments/paypal/capture", json={"paypal_order_id": order_id}, headers=headers
    )

    resp = await async_client.post(
        "/mobile/payments/paypal/webhook",
        content=_webhook_event("PAYMENT.CAPTURE.REFUNDED", order_id),
        headers={"content-type": "application/json"},
    )
    assert resp.status_code == 200

    status_resp = await async_client.get("/mobile/payments/status", headers=headers)
    assert status_resp.json()["status"] == "cancelled"


async def test_webhook_unmatched_order_id_is_a_no_op(
    async_client: AsyncClient, db_session: AsyncSession, fake_paypal
):
    resp = await async_client.post(
        "/mobile/payments/paypal/webhook",
        content=_webhook_event("PAYMENT.CAPTURE.COMPLETED", "NEVER-CREATED"),
        headers={"content-type": "application/json"},
    )
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Not configured
# ---------------------------------------------------------------------------


async def test_create_order_fails_loudly_when_paypal_not_configured(
    async_client: AsyncClient, db_session: AsyncSession
):
    """No PAYPAL_* settings are configured in the test environment and
    this test deliberately does NOT use the fake_paypal fixture —
    confirming the real, unconfigured path fails with a clear 500 rather
    than silently no-op'ing (there's no sensible degraded mode for "can't
    actually charge the user")."""
    headers = await _auth_headers(async_client, db_session, "notconfigured@example.com")
    resp = await async_client.post(
        "/mobile/payments/paypal/create-order", json={"plan": "monthly"}, headers=headers
    )
    assert resp.status_code == 500
    assert resp.json()["code"] == "paypal_not_configured"
