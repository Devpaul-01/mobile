"""Tests for app/services/smtp.py."""

from __future__ import annotations

import smtplib

import pytest

from app.core.config import settings
from app.services import smtp as smtp_module

pytestmark = pytest.mark.asyncio


async def test_send_email_without_smtp_host_logs_and_succeeds(monkeypatch):
    """With SMTP_HOST unset (the default in tests/CI/local dev), send_email
    must not attempt a real connection and must report success — this is
    the behavior auth_tokens.py and email.py both depend on to keep
    working without a configured mail server."""
    monkeypatch.setattr(settings, "smtp_host", None)

    def _fail_if_called(*args, **kwargs):  # pragma: no cover - should not run
        raise AssertionError("smtplib.SMTP should not be called when SMTP_HOST is unset")

    monkeypatch.setattr(smtp_module.smtplib, "SMTP", _fail_if_called)

    result = await smtp_module.send_email(
        to_email="someone@example.com", subject="Subject", body="Body"
    )
    assert result is True


async def test_send_email_with_smtp_host_attempts_real_send(monkeypatch):
    """With SMTP_HOST configured, send_email must actually drive smtplib
    (connect, optionally STARTTLS/login, send the message) rather than
    silently logging."""
    monkeypatch.setattr(settings, "smtp_host", "smtp.example.com")
    monkeypatch.setattr(settings, "smtp_port", 587)
    monkeypatch.setattr(settings, "smtp_username", "user")
    monkeypatch.setattr(settings, "smtp_password", "pass")
    monkeypatch.setattr(settings, "smtp_use_tls", True)

    calls: dict[str, object] = {}

    class _FakeSMTP:
        def __init__(self, host, port, timeout=10):
            calls["host"] = host
            calls["port"] = port

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def ehlo(self):
            calls["ehlo"] = calls.get("ehlo", 0) + 1

        def starttls(self):
            calls["starttls"] = True

        def login(self, username, password):
            calls["login"] = (username, password)

        def send_message(self, message):
            calls["sent_to"] = message["To"]
            calls["sent_subject"] = message["Subject"]

    monkeypatch.setattr(smtp_module.smtplib, "SMTP", _FakeSMTP)

    result = await smtp_module.send_email(
        to_email="someone@example.com", subject="Hello", body="Body text"
    )

    assert result is True
    assert calls["host"] == "smtp.example.com"
    assert calls["starttls"] is True
    assert calls["login"] == ("user", "pass")
    assert calls["sent_to"] == "someone@example.com"
    assert calls["sent_subject"] == "Hello"


async def test_send_email_propagates_failure_when_configured(monkeypatch):
    """A real SMTP error must propagate to the caller (so auth_tokens.py's
    retry/backoff loop sees it) rather than being swallowed."""
    monkeypatch.setattr(settings, "smtp_host", "smtp.example.com")

    def _raise(*args, **kwargs):
        raise smtplib.SMTPConnectError(421, "nope")

    monkeypatch.setattr(smtp_module.smtplib, "SMTP", _raise)

    with pytest.raises(smtplib.SMTPConnectError):
        await smtp_module.send_email(
            to_email="someone@example.com", subject="Hello", body="Body"
        )
