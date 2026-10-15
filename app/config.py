from __future__ import annotations

import os
from dataclasses import dataclass
from email.utils import parseaddr
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    app_env: str
    database_path: Path
    seed_demo_data: bool
    admin_email: str
    admin_name: str
    admin_password: str
    session_secret: str
    session_cookie: str
    session_ttl_hours: int
    cookie_secure: bool
    smtp_host: str
    smtp_port: int
    smtp_username: str
    smtp_password: str
    smtp_from: str
    smtp_reply_to: str
    smtp_starttls: bool
    smtp_ssl: bool
    smtp_timeout: int

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv()
        app_env = os.getenv("APP_ENV", "development").strip().lower()
        return cls(
            app_env=app_env,
            database_path=Path(os.getenv("DATABASE_PATH", "./data/minima-support.sqlite3")).expanduser().resolve(),
            seed_demo_data=_env_bool("SEED_DEMO_DATA", app_env != "production"),
            admin_email=os.getenv("ADMIN_EMAIL", "admin@minima.app").strip(),
            admin_name=os.getenv("ADMIN_NAME", "Алексей Крылов").strip() or "Администратор Minima",
            admin_password=os.getenv("ADMIN_PASSWORD", ""),
            session_secret=os.getenv("SESSION_SECRET", ""),
            session_cookie=os.getenv("SESSION_COOKIE", "minima_session").strip() or "minima_session",
            session_ttl_hours=_env_int("SESSION_TTL_HOURS", 8),
            cookie_secure=_env_bool("COOKIE_SECURE", app_env == "production"),
            smtp_host=os.getenv("SMTP_HOST", "").strip(),
            smtp_port=_env_int("SMTP_PORT", 587),
            smtp_username=os.getenv("SMTP_USERNAME", "").strip(),
            smtp_password=os.getenv("SMTP_PASSWORD", ""),
            smtp_from=os.getenv("SMTP_FROM", "").strip(),
            smtp_reply_to=os.getenv("SMTP_REPLY_TO", "").strip(),
            smtp_starttls=_env_bool("SMTP_STARTTLS", True),
            smtp_ssl=_env_bool("SMTP_SSL", False),
            smtp_timeout=_env_int("SMTP_TIMEOUT", 12),
        )

    @property
    def smtp_configured(self) -> bool:
        return bool(self.smtp_host and self.smtp_from)

    def validate(self) -> None:
        if self.app_env not in {"development", "test", "production"}:
            raise RuntimeError("APP_ENV должен быть development, test или production.")
        if not self.admin_email or "@" not in self.admin_email:
            raise RuntimeError("Задайте корректный ADMIN_EMAIL в .env.")
        if len(self.admin_password) < 12:
            raise RuntimeError("Задайте ADMIN_PASSWORD длиной не менее 12 символов в .env.")
        if len(self.session_secret) < 32:
            raise RuntimeError("Задайте SESSION_SECRET длиной не менее 32 символов в .env.")
        if self.app_env == "production" and not self.cookie_secure:
            raise RuntimeError("В APP_ENV=production установите COOKIE_SECURE=true.")
        if self.session_ttl_hours < 1 or self.session_ttl_hours > 168:
            raise RuntimeError("SESSION_TTL_HOURS должен быть от 1 до 168.")
        if self.smtp_port < 1 or self.smtp_port > 65535:
            raise RuntimeError("SMTP_PORT должен быть в диапазоне 1–65535.")
        if self.smtp_timeout < 1 or self.smtp_timeout > 120:
            raise RuntimeError("SMTP_TIMEOUT должен быть от 1 до 120 секунд.")
        if self.smtp_ssl and self.smtp_starttls:
            raise RuntimeError("Выберите только один режим SMTP: SMTP_SSL или STARTTLS.")
        if bool(self.smtp_username) != bool(self.smtp_password):
            raise RuntimeError("SMTP_USERNAME и SMTP_PASSWORD задаются вместе.")
        for name, address in (("SMTP_FROM", self.smtp_from), ("SMTP_REPLY_TO", self.smtp_reply_to)):
            if address and ("\r" in address or "\n" in address or not parseaddr(address)[1]):
                raise RuntimeError(f"{name} должен содержать корректный адрес без переводов строк.")


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise RuntimeError(f"Переменная {name} должна быть целым числом.") from exc
