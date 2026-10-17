"""Tests for app/services/push.py."""

from __future__ import annotations

import httpx
import pytest

from app.core.config import settings
from app.services import push as push_module

pytestmark = pytest.mark.asyncio


class _FakeResponse:
    def __init__(self, json_data: dict, status_code: int = 200):
        self._json_data = json_data
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("error", request=None, response=self)  # type: ignore[arg-type]

    def json(self) -> dict:
        return self._json_data


async def test_send_push_notification_posts_to_expo(monkeypatch):
    captured: dict[str, object] = {}

    class _FakeAsyncClient:
        def __init__(self, timeout=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, json=None, headers=None):
            captured["url"] = url
            captured["json"] = json
            captured["headers"] = headers
            return _FakeResponse({"data": {"status": "ok"}})

    monkeypatch.setattr(push_module.httpx, "AsyncClient", _FakeAsyncClient)

    await push_module.send_push_notification(
        device_token="ExponentPushToken[abc123]", contact_name="Alex"
    )

    assert captured["url"] == settings.expo_push_api_url
    assert captured["json"]["to"] == "ExponentPushToken[abc123]"
    assert "Alex" in captured["json"]["body"]


async def test_send_push_notification_skips_non_expo_tokens(monkeypatch):
    def _fail_if_called(*args, **kwargs):  # pragma: no cover - should not run
        raise AssertionError("httpx.AsyncClient should not be constructed for a non-Expo token")

    monkeypatch.setattr(push_module.httpx, "AsyncClient", _fail_if_called)

    # Should not raise, just log and return.
    await push_module.send_push_notification(device_token="not-a-real-token", contact_name="Alex")


async def test_send_push_notification_swallows_http_errors(monkeypatch):
    class _RaisingAsyncClient:
        def __init__(self, timeout=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, json=None, headers=None):
            raise httpx.ConnectError("boom")

    monkeypatch.setattr(push_module.httpx, "AsyncClient", _RaisingAsyncClient)

    # Fire-and-forget: must not raise even when the request fails outright.
    await push_module.send_push_notification(
        device_token="ExponentPushToken[abc123]", contact_name="Alex"
    )


async def test_send_approval_queue_push_posts_distinct_message(monkeypatch):
    captured: dict[str, object] = {}

    class _FakeAsyncClient:
        def __init__(self, timeout=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, json=None, headers=None):
            captured["json"] = json
            return _FakeResponse({"data": {"status": "ok"}})

    monkeypatch.setattr(push_module.httpx, "AsyncClient", _FakeAsyncClient)

    await push_module.send_approval_queue_push(
        device_token="ExponentPushToken[xyz]", contact_name="Sam"
    )

    assert "approval" in captured["json"]["title"].lower()
    assert "Sam" in captured["json"]["body"]
