from __future__ import annotations

import smtplib
from pathlib import Path

import pytest

from app.config import Settings
from app.mailer import MailDeliveryError, MailNotConfigured, send_support_email


def mail_settings(tmp_path: Path, *, configured: bool = True) -> Settings:
    return Settings(
        app_env="test",
        database_path=tmp_path / "db.sqlite3",
        seed_demo_data=False,
        admin_email="admin@minima.test",
        admin_name="Тестовый администратор",
        admin_password="Long-test-password-2026",
        session_secret="z" * 64,
        session_cookie="test_session",
        session_ttl_hours=8,
        cookie_secure=False,
        smtp_host="mail.minima.invalid" if configured else "",
        smtp_port=587,
        smtp_username="relay-user" if configured else "",
        smtp_password="relay-password" if configured else "",
        smtp_from="Minima Support <support@minima.invalid>" if configured else "",
        smtp_reply_to="reply@minima.invalid" if configured else "",
        smtp_starttls=True,
        smtp_ssl=False,
        smtp_timeout=3,
    )


def test_smtp_message_uses_tls_and_expected_headers(tmp_path, monkeypatch):
    from app import mailer

    calls = {}

    class FakeSMTP:
        def __init__(self, host, port, timeout):
            calls.update(host=host, port=port, timeout=timeout)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            calls["closed"] = True

        def ehlo(self):
            calls["ehlo"] = calls.get("ehlo", 0) + 1

        def starttls(self, *, context):
            calls["starttls"] = context is not None

        def login(self, username, password):
            calls["login"] = (username, password)

        def send_message(self, message):
            calls["message"] = message

    monkeypatch.setattr(mailer.smtplib, "SMTP", FakeSMTP)
    send_support_email(
        mail_settings(tmp_path),
        recipient="customer@example.invalid",
        ticket_id="SUP-4900",
        subject="Вопрос по оплате",
        body="Здравствуйте! Мы проверили платёж.",
    )
    message = calls["message"]
    assert calls["host"] == "mail.minima.invalid"
    assert calls["starttls"] is True
    assert calls["login"] == ("relay-user", "relay-password")
    assert calls["closed"] is True
    assert message["To"] == "customer@example.invalid"
    assert message["Subject"] == "[SUP-4900] Вопрос по оплате"
    assert message["Reply-To"] == "reply@minima.invalid"
    assert "Здравствуйте! Мы проверили платёж." in message.get_content()
    assert "SUP-4900" in message.get_content()


def test_missing_smtp_configuration_is_explicit(tmp_path):
    cfg = mail_settings(tmp_path, configured=False)
    with pytest.raises(MailNotConfigured):
        send_support_email(cfg, recipient="customer@example.invalid", ticket_id="SUP-4900", subject="Тема", body="Текст")


def test_smtp_exception_is_sanitized(tmp_path, monkeypatch):
    from app import mailer

    class FailingSMTP:
        def __init__(self, *_args, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def ehlo(self):
            return None

        def starttls(self, *, context):
            return None

        def login(self, *_args):
            return None

        def send_message(self, _message):
            raise smtplib.SMTPAuthenticationError(535, b"private provider response")

    monkeypatch.setattr(mailer.smtplib, "SMTP", FailingSMTP)
    with pytest.raises(MailDeliveryError) as caught:
        send_support_email(
            mail_settings(tmp_path), recipient="customer@example.invalid", ticket_id="SUP-4900", subject="Тема", body="Текст"
        )
    assert str(caught.value) == "SMTPAuthenticationError"
    assert "private provider response" not in str(caught.value)
