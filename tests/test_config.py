"""Проверки конфигурации: запуск в продакшене должен быть безопасным по умолчанию."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from app.config import Settings


def production_settings(tmp_path: Path, **overrides) -> Settings:
    base = Settings(
        app_env="production",
        database_path=tmp_path / "prod.sqlite3",
        seed_demo_data=False,
        admin_email="support@minima.invalid",
        admin_name="Администратор",
        admin_password="Strong-production-password-2026",
        session_secret="p" * 64,
        session_cookie="minima_session",
        session_ttl_hours=8,
        cookie_secure=True,
        smtp_host="smtp.minima.invalid",
        smtp_port=587,
        smtp_username="support@minima.invalid",
        smtp_password="smtp-app-password",
        smtp_from="Minima Support <support@minima.invalid>",
        smtp_reply_to="support@minima.invalid",
        smtp_starttls=True,
        smtp_ssl=False,
        smtp_timeout=12,
    )
    return replace(base, **overrides)


def test_production_settings_pass_validation(tmp_path):
    production_settings(tmp_path).validate()


def test_production_requires_secure_cookies(tmp_path):
    with pytest.raises(RuntimeError, match="COOKIE_SECURE"):
        production_settings(tmp_path, cookie_secure=False).validate()


def test_dev_email_codes_are_forbidden_in_production(tmp_path):
    with pytest.raises(RuntimeError, match="DEV_LOG_EMAIL_CODES"):
        production_settings(tmp_path, dev_log_email_codes=True).validate()


def test_email_code_limits_are_validated(tmp_path):
    with pytest.raises(RuntimeError, match="EMAIL_CODE_TTL_MINUTES"):
        production_settings(tmp_path, email_code_ttl_minutes=0).validate()
    with pytest.raises(RuntimeError, match="EMAIL_CODE_LENGTH"):
        production_settings(tmp_path, email_code_length=3).validate()
    with pytest.raises(RuntimeError, match="EMAIL_CODE_MAX_ATTEMPTS"):
        production_settings(tmp_path, email_code_max_attempts=100).validate()
    with pytest.raises(RuntimeError, match="EMAIL_CODE_RESEND_COOLDOWN_SECONDS"):
        production_settings(tmp_path, email_code_resend_cooldown_seconds=0).validate()


def test_smtp_credentials_are_set_together(tmp_path):
    with pytest.raises(RuntimeError, match="SMTP_USERNAME"):
        production_settings(tmp_path, smtp_password="").validate()
    with pytest.raises(RuntimeError, match="SMTP_USERNAME"):
        production_settings(tmp_path, smtp_username="").validate()
    # Без аутентификации (локальный приёмник) — допустимо: оба поля пустые.
    production_settings(tmp_path, smtp_username="", smtp_password="").validate()


def test_short_admin_password_is_rejected(tmp_path):
    with pytest.raises(RuntimeError, match="ADMIN_PASSWORD"):
        production_settings(tmp_path, admin_password="short").validate()
