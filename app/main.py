from __future__ import annotations

import hashlib
import hmac
import json
import logging
import math
import re
import secrets
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from email.utils import parseaddr
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.config import Settings
from app.database import connection, init_database, iso_now, to_iso, utc_now
from app.mailer import MailDeliveryError, MailNotConfigured, send_support_email, send_verification_email
from app.security import (
    clean_text,
    decode_salt,
    encode_salt,
    generate_code,
    hash_code,
    hash_password,
    ip_hash,
    new_salt,
    password_problem,
    rate_key_hash,
    verify_code,
    verify_password,
)

logger = logging.getLogger("minima.support")
PRIORITIES = {
    "urgent": (15, 4 * 60),
    "high": (60, 8 * 60),
    "normal": (4 * 60, 24 * 60),
    "low": (8 * 60, 72 * 60),
}
PRIORITY_LABELS = {"urgent": "критический", "high": "высокий", "normal": "обычный", "low": "низкий"}
EMAIL_RE = re.compile(r"[^@\s]+@(?:[A-Za-z0-9-]+\.)+[A-Za-z0-9-]{2,63}\Z")
INDEX_FILE = Path(__file__).resolve().parent.parent / "index.html"
CODE_PURPOSE = "signup"
ROLE_ADMIN = "admin"
ROLE_OWNER = "owner"


class LoginBody(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=256)


class RegisterRequestBody(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    organization: str = Field(min_length=2, max_length=80)
    name: str = Field(min_length=2, max_length=80)
    email: str = Field(min_length=5, max_length=254)
    password: str = Field(min_length=1, max_length=256)

    @field_validator("organization", "name")
    @classmethod
    def validate_text(cls, value: str) -> str:
        cleaned = clean_text(value)
        if len(cleaned) < 2:
            raise ValueError("Заполните поле: не менее 2 символов.")
        return cleaned

    @field_validator("email")
    @classmethod
    def validate_email(cls, value: str) -> str:
        return _normalize_email(value)


class ResendCodeBody(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    email: str = Field(min_length=5, max_length=254)

    @field_validator("email")
    @classmethod
    def validate_email(cls, value: str) -> str:
        return _normalize_email(value)


class ConfirmCodeBody(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    email: str = Field(min_length=5, max_length=254)
    code: str = Field(min_length=4, max_length=8)

    @field_validator("email")
    @classmethod
    def validate_email(cls, value: str) -> str:
        return _normalize_email(value)

    @field_validator("code")
    @classmethod
    def validate_code(cls, value: str) -> str:
        code = re.sub(r"\s+", "", value)
        if not code.isdigit():
            raise ValueError("Код состоит только из цифр.")
        return code


class TicketCreateBody(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    company: str = Field(min_length=1, max_length=80)
    contact: str = Field(min_length=1, max_length=80)
    email: str = Field(min_length=5, max_length=254)
    plan: str = Field(default="Бизнес", min_length=1, max_length=40)
    subject: str = Field(min_length=3, max_length=120)
    category: str = Field(default="Другое", min_length=1, max_length=60)
    priority: Literal["urgent", "high", "normal", "low"] = "normal"
    message: str = Field(min_length=1, max_length=1500)

    @field_validator("email")
    @classmethod
    def validate_email(cls, value: str) -> str:
        if "\r" in value or "\n" in value or parseaddr(value)[1] != value or not EMAIL_RE.fullmatch(value):
            raise ValueError("Укажите корректный адрес электронной почты.")
        return value

    @field_validator("subject", "company", "contact", "category", "plan")
    @classmethod
    def validate_text(cls, value: str) -> str:
        cleaned = clean_text(value)
        if not cleaned:
            raise ValueError("Поле не должно быть пустым.")
        return cleaned

    @field_validator("subject")
    @classmethod
    def validate_subject_header(cls, value: str) -> str:
        if "\r" in value or "\n" in value:
            raise ValueError("Тема обращения не должна содержать переводов строк.")
        return value


class TicketPatchBody(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    status: Literal["new", "in_progress", "waiting", "resolved"] | None = None
    priority: Literal["urgent", "high", "normal", "low"] | None = None
    assigned: str | None = Field(default=None, max_length=80)


class MessageCreateBody(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    kind: Literal["support", "note"]
    text: str = Field(min_length=1, max_length=2000)


def _normalize_email(value: str) -> str:
    candidate = clean_text(value).casefold()
    if "\r" in candidate or "\n" in candidate or parseaddr(candidate)[1] != candidate or not EMAIL_RE.fullmatch(candidate):
        raise ValueError("Укажите корректный адрес электронной почты.")
    return candidate


def _parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _validate_origin(request: Request) -> None:
    origin = request.headers.get("origin")
    if not origin:
        return  # Non-browser clients (for example curl) do not send Origin.
    if origin == "null":
        raise HTTPException(status_code=403, detail="Запросы с непрозрачного источника запрещены.")
    origin_host = urlsplit(origin).netloc.lower()
    request_host = request.headers.get("host", "").lower()
    if not origin_host or not request_host or origin_host != request_host:
        raise HTTPException(status_code=403, detail="Источник запроса не совпадает с консолью.")


def _session_hash(token: str, cfg: Settings) -> str:
    return hmac.new(cfg.session_secret.encode("utf-8"), token.encode("utf-8"), hashlib.sha256).hexdigest()


def _current_user(request: Request, cfg: Settings) -> dict[str, str] | None:
    """Return the authenticated identity: platform admin or organization owner."""
    token = request.cookies.get(cfg.session_cookie)
    if not token:
        return None
    token_hash = _session_hash(token, cfg)
    now_iso = iso_now()
    with connection(cfg.database_path) as conn:
        row = conn.execute(
            "SELECT email, name, role, organization, expires_at FROM sessions WHERE token_hash = ?",
            (token_hash,),
        ).fetchone()
        if not row:
            return None
        if row["expires_at"] <= now_iso:
            conn.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))
            return None
        return {
            "email": row["email"],
            "name": row["name"],
            "role": row["role"] or ROLE_ADMIN,
            "organization": row["organization"] or "",
        }


def _require_user(request: Request, cfg: Settings) -> dict[str, str]:
    user = _current_user(request, cfg)
    if not user:
        raise HTTPException(status_code=401, detail="Требуется войти в консоль Minima.")
    return user


def _require_admin(request: Request, cfg: Settings) -> dict[str, str]:
    user = _require_user(request, cfg)
    if user["role"] != ROLE_ADMIN:
        raise HTTPException(status_code=403, detail="Действие доступно только администратору платформы.")
    return user


def _create_session(
    cfg: Settings,
    response: Response,
    *,
    email: str,
    name: str,
    role: str,
    organization: str = "",
    user_id: int | None = None,
    now: datetime | None = None,
) -> dict[str, str]:
    now = now or utc_now()
    token = secrets.token_urlsafe(36)
    expiry = now + timedelta(hours=cfg.session_ttl_hours)
    with connection(cfg.database_path) as conn:
        conn.execute("DELETE FROM sessions WHERE expires_at <= ?", (to_iso(now),))
        conn.execute(
            """INSERT INTO sessions (token_hash, email, name, role, organization, user_id, created_at, expires_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                _session_hash(token, cfg),
                email.casefold(),
                name,
                role,
                organization,
                user_id,
                to_iso(now),
                to_iso(expiry),
            ),
        )
    response.set_cookie(
        key=cfg.session_cookie,
        value=token,
        max_age=cfg.session_ttl_hours * 60 * 60,
        expires=expiry,
        httponly=True,
        secure=cfg.cookie_secure,
        samesite="strict",
        path="/",
    )
    return {"email": email.casefold(), "name": name, "role": role, "organization": organization}


def _serialize_ticket(conn: sqlite3.Connection, ticket_id: str, *, viewer: dict[str, str] | None = None) -> dict:
    ticket = conn.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
    if not ticket:
        raise HTTPException(status_code=404, detail="Обращение не найдено.")
    rows = conn.execute(
        "SELECT id, kind, author, body, created_at, delivery_status FROM messages WHERE ticket_id = ? ORDER BY id",
        (ticket_id,),
    ).fetchall()
    # Internal notes stay inside the support team: an organization owner never sees them.
    if viewer and viewer["role"] != ROLE_ADMIN:
        rows = [item for item in rows if item["kind"] != "note"]
    return {
        "id": ticket["id"],
        "company": ticket["company"],
        "contact": ticket["contact"],
        "email": ticket["email"],
        "plan": ticket["plan"],
        "subject": ticket["subject"],
        "category": ticket["category"],
        "priority": ticket["priority"],
        "status": ticket["status"],
        "assigned": ticket["assigned"],
        "createdAt": ticket["created_at"],
        "firstResponseDue": ticket["first_response_due"],
        "resolutionDue": ticket["resolution_due"],
        "firstRespondedAt": ticket["first_responded_at"],
        "resolvedAt": ticket["resolved_at"],
        "updatedAt": ticket["updated_at"],
        "version": ticket["version"],
        "messages": [
            {
                "id": item["id"],
                "kind": item["kind"],
                "author": item["author"],
                "text": item["body"],
                "at": item["created_at"],
                "deliveryStatus": item["delivery_status"],
            }
            for item in rows
        ],
    }


def _ticket_visible_to(ticket_row: sqlite3.Row, user: dict[str, str]) -> bool:
    if user["role"] == ROLE_ADMIN:
        return True
    return ticket_row["company"].strip().casefold() == user["organization"].strip().casefold()


def _append_message(
    conn: sqlite3.Connection,
    *,
    ticket_id: str,
    kind: str,
    author: str,
    body: str,
    created_at: str | None = None,
    delivery_status: str = "not_applicable",
    idempotency_key: str | None = None,
) -> int:
    cursor = conn.execute(
        """INSERT INTO messages (ticket_id, kind, author, body, created_at, delivery_status, idempotency_key)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (ticket_id, kind, author, body, created_at or iso_now(), delivery_status, idempotency_key),
    )
    return int(cursor.lastrowid)


def _throttle(
    conn: sqlite3.Connection,
    *,
    cfg: Settings,
    bucket: str,
    key: str,
    limit: int,
    window_seconds: int,
    now: datetime,
) -> int:
    """Count a request in `bucket`. Return how many seconds to wait (0 when allowed)."""
    key_hash = rate_key_hash(bucket, key, secret=cfg.session_secret)
    row = conn.execute(
        "SELECT window_started_at, count FROM rate_limits WHERE bucket = ? AND key_hash = ?",
        (bucket, key_hash),
    ).fetchone()
    now_iso = to_iso(now)
    if row is None:
        conn.execute(
            "INSERT INTO rate_limits (bucket, key_hash, window_started_at, window_seconds, count) VALUES (?, ?, ?, ?, 1)",
            (bucket, key_hash, now_iso, window_seconds),
        )
        return 0
    elapsed = (now - _parse_iso(row["window_started_at"])).total_seconds()
    if elapsed >= window_seconds:
        conn.execute(
            "UPDATE rate_limits SET window_started_at = ?, window_seconds = ?, count = 1 WHERE bucket = ? AND key_hash = ?",
            (now_iso, window_seconds, bucket, key_hash),
        )
        return 0
    count = int(row["count"]) + 1
    conn.execute(
        "UPDATE rate_limits SET window_seconds = ?, count = ? WHERE bucket = ? AND key_hash = ?",
        (window_seconds, count, bucket, key_hash),
    )
    if count > limit:
        return max(1, math.ceil(window_seconds - elapsed))
    return 0


def _purge_expired(conn: sqlite3.Connection, now: datetime) -> None:
    now_iso = to_iso(now)
    conn.execute("DELETE FROM email_verification_codes WHERE expires_at < ?", (to_iso(now - timedelta(hours=24)),))
    conn.execute(
        """DELETE FROM rate_limits
           WHERE julianday(?) > julianday(window_started_at, '+' || window_seconds || ' seconds')""",
        (now_iso,),
    )


def _latest_code(conn: sqlite3.Connection, email: str) -> sqlite3.Row | None:
    return conn.execute(
        """SELECT * FROM email_verification_codes
           WHERE email = ? AND purpose = ? AND consumed_at IS NULL
           ORDER BY id DESC LIMIT 1""",
        (email, CODE_PURPOSE),
    ).fetchone()


def _consume_codes(conn: sqlite3.Connection, email: str, now_iso: str) -> None:
    conn.execute(
        "UPDATE email_verification_codes SET consumed_at = ? WHERE email = ? AND purpose = ? AND consumed_at IS NULL",
        (now_iso, email, CODE_PURPOSE),
    )


def _deliver_signup_code(cfg: Settings, *, email: str, name: str, now: datetime) -> tuple[str, datetime, str]:
    """Create and deliver a one-time code. Returns (code, expires_at, status)."""
    code = generate_code(cfg.email_code_length)
    expires_at = now + timedelta(minutes=cfg.email_code_ttl_minutes)
    if cfg.smtp_configured:
        try:
            send_verification_email(
                cfg,
                recipient=email,
                code=code,
                ttl_minutes=cfg.email_code_ttl_minutes,
                name=name,
            )
        except MailNotConfigured as exc:
            raise HTTPException(
                status_code=503,
                detail="SMTP не настроен. Код не отправлен и учётная запись не создана.",
            ) from exc
        except MailDeliveryError as exc:
            raise HTTPException(
                status_code=502,
                detail="Почтовый сервер не принял письмо с кодом. Попробуйте запросить код позже.",
            ) from exc
        return code, expires_at, "sent"
    if cfg.dev_log_email_codes:
        # Local development only. The code never leaves the server log and no email is sent.
        logger.warning(
            "Код подтверждения для %s: %s (действует до %s). SMTP не настроен, письмо НЕ отправлено.",
            email,
            code,
            to_iso(expires_at),
        )
        return code, expires_at, "logged"
    raise HTTPException(
        status_code=503,
        detail=(
            "Отправка кода невозможна: SMTP не настроен. Заполните SMTP_HOST и SMTP_FROM в .env на сервере — "
            "письмо не отправлено и учётная запись не создана."
        ),
    )


def _code_response(cfg: Settings, *, status_value: str, email: str) -> dict:
    if status_value == "sent":
        message = f"Код отправлен на {email}. Введите его, чтобы завершить регистрацию."
        delivery = "email"
    else:
        message = "SMTP не настроен: письмо НЕ отправлено. Код выведен в лог сервера (режим разработки DEV_LOG_EMAIL_CODES=true)."
        delivery = "server-log"
    return {
        "status": status_value,
        "delivery": delivery,
        "email": email,
        "expiresInSeconds": cfg.email_code_ttl_minutes * 60,
        "codeLength": cfg.email_code_length,
        "maxAttempts": cfg.email_code_max_attempts,
        "resendAvailableInSeconds": cfg.email_code_resend_cooldown_seconds,
        "message": message,
    }


def create_app(settings: Settings | None = None) -> FastAPI:
    cfg = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        cfg.validate()
        init_database(cfg.database_path, cfg.seed_demo_data)
        yield

    application = FastAPI(
        title="Minima Support API",
        version="1.1.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url="/api/openapi.json",
    )

    @application.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; font-src 'self' data:; connect-src 'self'; object-src 'none'; "
            "base-uri 'self'; form-action 'self'"
        )
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @application.get("/healthz")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @application.get("/", include_in_schema=False)
    @application.get("/index.html", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(INDEX_FILE, media_type="text/html; charset=utf-8")

    @application.get("/api/v1/auth/session")
    def session(request: Request) -> dict:
        user = _current_user(request, cfg)
        return {
            "authenticated": bool(user),
            "admin": {"email": user["email"], "name": user["name"]} if user and user["role"] == ROLE_ADMIN else None,
            "user": user,
            "role": user["role"] if user else None,
            "organization": user["organization"] if user else "",
            "smtpConfigured": cfg.smtp_configured,
            "signupEnabled": cfg.signup_enabled,
        }

    @application.post("/api/v1/auth/login")
    def login(payload: LoginBody, request: Request, response: Response) -> dict:
        _validate_origin(request)
        email_key = payload.email.casefold()
        is_admin = hmac.compare_digest(email_key, cfg.admin_email.casefold()) and hmac.compare_digest(
            payload.password, cfg.admin_password
        )
        user_row = None
        if not is_admin:
            with connection(cfg.database_path) as conn:
                user_row = conn.execute("SELECT * FROM users WHERE email = ?", (email_key,)).fetchone()
        identity: dict[str, str] | None = None
        if is_admin:
            identity = {"email": cfg.admin_email.casefold(), "name": cfg.admin_name, "role": ROLE_ADMIN, "organization": ""}
        elif user_row is not None and verify_password(payload.password, user_row["password_hash"], secret=cfg.session_secret):
            identity = {
                "email": user_row["email"],
                "name": user_row["name"],
                "role": user_row["role"],
                "organization": user_row["organization"],
            }
        credentials_ok = identity is not None
        client_ip = request.client.host if request.client else "unknown"
        login_ip_hash = ip_hash(client_ip, secret=cfg.session_secret)
        now = utc_now()
        now_iso = to_iso(now)
        throttle_seconds = 15 * 60
        blocked_for = 0
        invalid_credentials = False
        token = secrets.token_urlsafe(36) if credentials_ok else ""
        expiry = now + timedelta(hours=cfg.session_ttl_hours)
        with connection(cfg.database_path) as conn:
            throttle = conn.execute("SELECT window_started_at, attempts FROM login_throttles WHERE ip_hash = ?", (login_ip_hash,)).fetchone()
            if throttle:
                elapsed = (now - _parse_iso(throttle["window_started_at"])).total_seconds()
                if elapsed < throttle_seconds and throttle["attempts"] >= 5:
                    blocked_for = max(1, int(throttle_seconds - elapsed))
                elif not credentials_ok and elapsed < throttle_seconds:
                    conn.execute("UPDATE login_throttles SET attempts = attempts + 1 WHERE ip_hash = ?", (login_ip_hash,))
                    invalid_credentials = True
                elif not credentials_ok:
                    conn.execute("UPDATE login_throttles SET window_started_at = ?, attempts = 1 WHERE ip_hash = ?", (now_iso, login_ip_hash))
                    invalid_credentials = True
                else:
                    conn.execute("DELETE FROM login_throttles WHERE ip_hash = ?", (login_ip_hash,))
            elif not credentials_ok:
                conn.execute("INSERT INTO login_throttles (ip_hash, window_started_at, attempts) VALUES (?, ?, 1)", (login_ip_hash, now_iso))
                invalid_credentials = True

        if blocked_for:
            raise HTTPException(status_code=429, detail="Слишком много попыток входа. Попробуйте позже.", headers={"Retry-After": str(blocked_for)})
        if invalid_credentials:
            raise HTTPException(status_code=401, detail="Неверная почта или пароль.")

        account_wait = 0
        with connection(cfg.database_path) as conn:
            account_wait = _throttle(
                conn,
                cfg=cfg,
                bucket="login-account",
                key=email_key,
                limit=10,
                window_seconds=throttle_seconds,
                now=now,
            )
        if account_wait:
            raise HTTPException(
                status_code=429,
                detail="Слишком много попыток входа для этой почты. Попробуйте позже.",
                headers={"Retry-After": str(account_wait)},
            )

        assert identity is not None
        user = _create_session(
            cfg,
            response,
            email=identity["email"],
            name=identity["name"],
            role=identity["role"],
            organization=identity["organization"],
            user_id=(user_row["id"] if identity["role"] == ROLE_OWNER and user_row is not None else None),
            now=now,
        )
        if user_row is not None and identity["role"] == ROLE_OWNER:
            with connection(cfg.database_path) as conn:
                conn.execute("UPDATE users SET last_login_at = ? WHERE id = ?", (now_iso, user_row["id"]))
        return {
            "authenticated": True,
            "admin": {"email": identity["email"], "name": identity["name"]} if identity["role"] == ROLE_ADMIN else None,
            "user": user,
            "role": identity["role"],
            "smtpConfigured": cfg.smtp_configured,
        }

    @application.post("/api/v1/auth/logout")
    def logout(request: Request, response: Response) -> dict[str, bool]:
        _validate_origin(request)
        token = request.cookies.get(cfg.session_cookie)
        if token:
            with connection(cfg.database_path) as conn:
                conn.execute("DELETE FROM sessions WHERE token_hash = ?", (_session_hash(token, cfg),))
        response.delete_cookie(cfg.session_cookie, path="/", httponly=True, secure=cfg.cookie_secure, samesite="strict")
        return {"ok": True}

    @application.post("/api/v1/auth/register/request", status_code=status.HTTP_202_ACCEPTED)
    def register_request(payload: RegisterRequestBody, request: Request) -> dict:
        """Start registration: validate data and send a one-time email code."""
        _validate_origin(request)
        if not cfg.signup_enabled:
            raise HTTPException(status_code=404, detail="Регистрация владельцев организации отключена.")
        problem = password_problem(payload.password, cfg.password_min_length)
        if problem:
            raise HTTPException(status_code=422, detail=problem)
        email = payload.email
        client_ip = request.client.host if request.client else "unknown"
        now = utc_now()
        with connection(cfg.database_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            _purge_expired(conn, now)
            if conn.execute("SELECT 1 FROM users WHERE email = ?", (email,)).fetchone():
                raise HTTPException(
                    status_code=409,
                    detail="Учётная запись с этой почтой уже подтверждена. Войдите в консоль.",
                )
            email_wait = _throttle(
                conn,
                cfg=cfg,
                bucket="signup-email",
                key=email,
                limit=cfg.email_code_max_per_email_hour,
                window_seconds=3600,
                now=now,
            )
            if email_wait:
                raise HTTPException(
                    status_code=429,
                    detail=f"Слишком много запросов кода для этой почты. Попробуйте через {math.ceil(email_wait / 60)} мин.",
                    headers={"Retry-After": str(email_wait)},
                )
            ip_wait = _throttle(
                conn,
                cfg=cfg,
                bucket="signup-ip",
                key=client_ip,
                limit=cfg.email_code_max_per_ip_hour,
                window_seconds=3600,
                now=now,
            )
            if ip_wait:
                raise HTTPException(
                    status_code=429,
                    detail=f"Слишком много запросов кода с этого адреса. Попробуйте через {math.ceil(ip_wait / 60)} мин.",
                    headers={"Retry-After": str(ip_wait)},
                )
            last = conn.execute(
                "SELECT created_at FROM email_verification_codes WHERE email = ? ORDER BY id DESC LIMIT 1",
                (email,),
            ).fetchone()
            if last:
                cooldown_left = cfg.email_code_resend_cooldown_seconds - (now - _parse_iso(last["created_at"])).total_seconds()
                if cooldown_left > 0:
                    seconds = math.ceil(cooldown_left)
                    raise HTTPException(
                        status_code=429,
                        detail=f"Новый код можно запросить через {seconds} с.",
                        headers={"Retry-After": str(seconds)},
                    )
            _consume_codes(conn, email, to_iso(now))

        code, expires_at, status_value = _deliver_signup_code(cfg, email=email, name=payload.name, now=now)
        salt = new_salt()
        draft = json.dumps(
            {
                "organization": payload.organization,
                "name": payload.name,
                "password_hash": hash_password(payload.password, secret=cfg.session_secret),
            },
            ensure_ascii=False,
        )
        with connection(cfg.database_path) as conn:
            conn.execute(
                """INSERT INTO email_verification_codes
                   (email, purpose, code_hash, code_salt, draft_json, created_at, expires_at, attempts, request_ip_hash)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?)""",
                (
                    email,
                    CODE_PURPOSE,
                    hash_code(code, salt=salt),
                    encode_salt(salt),
                    draft,
                    to_iso(now),
                    to_iso(expires_at),
                    ip_hash(client_ip, secret=cfg.session_secret),
                ),
            )
        return _code_response(cfg, status_value=status_value, email=email)

    @application.post("/api/v1/auth/register/resend", status_code=status.HTTP_202_ACCEPTED)
    def register_resend(payload: ResendCodeBody, request: Request) -> dict:
        """Send a fresh code for an already started registration."""
        _validate_origin(request)
        if not cfg.signup_enabled:
            raise HTTPException(status_code=404, detail="Регистрация владельцев организации отключена.")
        email = payload.email
        client_ip = request.client.host if request.client else "unknown"
        now = utc_now()
        with connection(cfg.database_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            _purge_expired(conn, now)
            if conn.execute("SELECT 1 FROM users WHERE email = ?", (email,)).fetchone():
                raise HTTPException(status_code=409, detail="Учётная запись с этой почтой уже подтверждена. Войдите в консоль.")
            row = _latest_code(conn, email)
            if not row:
                raise HTTPException(status_code=404, detail="Активного запроса регистрации нет. Заполните форму регистрации заново.")
            email_wait = _throttle(conn, cfg=cfg, bucket="signup-email", key=email, limit=cfg.email_code_max_per_email_hour, window_seconds=3600, now=now)
            if email_wait:
                raise HTTPException(
                    status_code=429,
                    detail=f"Слишком много запросов кода для этой почты. Попробуйте через {math.ceil(email_wait / 60)} мин.",
                    headers={"Retry-After": str(email_wait)},
                )
            ip_wait = _throttle(conn, cfg=cfg, bucket="signup-ip", key=client_ip, limit=cfg.email_code_max_per_ip_hour, window_seconds=3600, now=now)
            if ip_wait:
                raise HTTPException(
                    status_code=429,
                    detail=f"Слишком много запросов кода с этого адреса. Попробуйте через {math.ceil(ip_wait / 60)} мин.",
                    headers={"Retry-After": str(ip_wait)},
                )
            cooldown_left = cfg.email_code_resend_cooldown_seconds - (now - _parse_iso(row["created_at"])).total_seconds()
            if cooldown_left > 0:
                seconds = math.ceil(cooldown_left)
                raise HTTPException(
                    status_code=429,
                    detail=f"Новый код можно запросить через {seconds} с.",
                    headers={"Retry-After": str(seconds)},
                )
            try:
                draft = json.loads(row["draft_json"])
                name = str(draft.get("name") or "")
            except (ValueError, TypeError, AttributeError):
                name = ""
            if not name:
                raise HTTPException(status_code=409, detail="Данные регистрации устарели. Заполните форму регистрации заново.")

        code, expires_at, status_value = _deliver_signup_code(cfg, email=email, name=name, now=now)
        salt = new_salt()
        with connection(cfg.database_path) as conn:
            conn.execute(
                """UPDATE email_verification_codes
                   SET code_hash = ?, code_salt = ?, created_at = ?, expires_at = ?, attempts = 0, request_ip_hash = ?
                   WHERE id = ?""",
                (
                    hash_code(code, salt=salt),
                    encode_salt(salt),
                    to_iso(now),
                    to_iso(expires_at),
                    ip_hash(client_ip, secret=cfg.session_secret),
                    row["id"],
                ),
            )
        return _code_response(cfg, status_value=status_value, email=email)

    @application.post("/api/v1/auth/register/confirm", status_code=status.HTTP_201_CREATED)
    def register_confirm(payload: ConfirmCodeBody, request: Request, response: Response) -> dict:
        """Confirm the code and create the organization owner account."""
        _validate_origin(request)
        if not cfg.signup_enabled:
            raise HTTPException(status_code=404, detail="Регистрация владельцев организации отключена.")
        email = payload.email
        client_ip = request.client.host if request.client else "unknown"
        now = utc_now()
        now_iso = to_iso(now)

        with connection(cfg.database_path) as conn:
            confirm_wait = _throttle(
                conn,
                cfg=cfg,
                bucket="confirm-ip",
                key=client_ip,
                limit=cfg.confirm_max_per_ip_hour,
                window_seconds=3600,
                now=now,
            )
        if confirm_wait:
            raise HTTPException(
                status_code=429,
                detail=f"Слишком много попыток подтверждения. Попробуйте через {math.ceil(confirm_wait / 60)} мин.",
                headers={"Retry-After": str(confirm_wait)},
            )

        with connection(cfg.database_path) as conn:
            row = _latest_code(conn, email)
        if not row:
            raise HTTPException(status_code=400, detail="Активного кода для этой почты нет. Запросите код заново.")

        if int(row["attempts"]) >= cfg.email_code_max_attempts:
            with connection(cfg.database_path) as conn:
                _consume_codes(conn, email, now_iso)
            raise HTTPException(status_code=429, detail="Слишком много неверных попыток. Запросите новый код.")

        if row["expires_at"] <= now_iso:
            with connection(cfg.database_path) as conn:
                _consume_codes(conn, email, now_iso)
            raise HTTPException(status_code=410, detail="Срок действия кода истёк. Запросите новый код.")

        if not verify_code(payload.code, salt=decode_salt(row["code_salt"]), expected=row["code_hash"]):
            attempts = int(row["attempts"]) + 1
            with connection(cfg.database_path) as conn:
                conn.execute("UPDATE email_verification_codes SET attempts = ? WHERE id = ?", (attempts, row["id"]))
                if attempts >= cfg.email_code_max_attempts:
                    _consume_codes(conn, email, now_iso)
            remaining = cfg.email_code_max_attempts - attempts
            if remaining <= 0:
                raise HTTPException(status_code=429, detail="Код больше не действителен. Запросите новый код.")
            raise HTTPException(status_code=400, detail=f"Неверный код. Осталось попыток: {remaining}.")

        try:
            draft = json.loads(row["draft_json"])
            organization = str(draft["organization"])
            name = str(draft["name"])
            password_hash = str(draft["password_hash"])
        except (ValueError, TypeError, KeyError, AttributeError):
            with connection(cfg.database_path) as conn:
                conn.execute("UPDATE email_verification_codes SET consumed_at = ? WHERE id = ?", (now_iso, row["id"]))
            raise HTTPException(status_code=400, detail="Данные регистрации повреждены. Заполните форму регистрации заново.")

        with connection(cfg.database_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute("SELECT 1 FROM users WHERE email = ?", (email,)).fetchone():
                _consume_codes(conn, email, now_iso)
                raise HTTPException(status_code=409, detail="Учётная запись с этой почтой уже подтверждена. Войдите в консоль.")
            cursor = conn.execute(
                """INSERT INTO users (email, name, organization, password_hash, role, email_verified_at, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (email, name, organization, password_hash, ROLE_OWNER, now_iso, now_iso),
            )
            user_id = int(cursor.lastrowid)
            _consume_codes(conn, email, now_iso)

        user = _create_session(
            cfg,
            response,
            email=email,
            name=name,
            role=ROLE_OWNER,
            organization=organization,
            user_id=user_id,
            now=now,
        )
        with connection(cfg.database_path) as conn:
            conn.execute("UPDATE users SET last_login_at = ? WHERE id = ?", (now_iso, user_id))
            conn.execute(
                "DELETE FROM rate_limits WHERE bucket IN ('signup-email', 'signup-ip') AND key_hash IN (?, ?)",
                (
                    rate_key_hash("signup-email", email, secret=cfg.session_secret),
                    rate_key_hash("signup-ip", client_ip, secret=cfg.session_secret),
                ),
            )
        return {
            "authenticated": True,
            "admin": None,
            "user": user,
            "role": ROLE_OWNER,
            "smtpConfigured": cfg.smtp_configured,
            "message": "Почта подтверждена. Учётная запись владельца организации создана.",
        }

    @application.get("/api/v1/support/tickets")
    def tickets(request: Request) -> dict:
        user = _require_user(request, cfg)
        with connection(cfg.database_path) as conn:
            rows = conn.execute(
                """SELECT * FROM tickets
                   ORDER BY CASE WHEN status = 'resolved' THEN 1 ELSE 0 END,
                     CASE WHEN first_responded_at IS NULL THEN first_response_due ELSE resolution_due END,
                     created_at DESC"""
            ).fetchall()
            visible = [row for row in rows if _ticket_visible_to(row, user)]
            return {"tickets": [_serialize_ticket(conn, row["id"], viewer=user) for row in visible]}

    @application.get("/api/v1/support/tickets/{ticket_id}")
    def ticket(ticket_id: str, request: Request) -> dict:
        user = _require_user(request, cfg)
        with connection(cfg.database_path) as conn:
            row = conn.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Обращение не найдено.")
            if not _ticket_visible_to(row, user):
                raise HTTPException(status_code=404, detail="Обращение не найдено.")
            return _serialize_ticket(conn, ticket_id, viewer=user)

    @application.post("/api/v1/support/tickets", status_code=status.HTTP_201_CREATED)
    def create_ticket(payload: TicketCreateBody, request: Request) -> dict:
        _validate_origin(request)
        user = _require_user(request, cfg)
        # An owner can only open tickets for their own organization.
        company = user["organization"] if user["role"] == ROLE_OWNER else payload.company
        contact = payload.contact or (user["name"] if user["role"] == ROLE_OWNER else "")
        now = utc_now()
        first_minutes, resolution_minutes = PRIORITIES[payload.priority]
        with connection(cfg.database_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            next_number = conn.execute(
                "SELECT COALESCE(MAX(CAST(SUBSTR(id, 5) AS INTEGER)), 4800) + 1 FROM tickets WHERE id LIKE 'SUP-%'"
            ).fetchone()[0]
            ticket_id = f"SUP-{int(next_number)}"
            conn.execute(
                """INSERT INTO tickets (
                     id, company, contact, email, plan, subject, category, priority, status, assigned,
                     created_at, first_response_due, resolution_due, updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, '', ?, ?, ?, ?)""",
                (
                    ticket_id, company, contact, payload.email.casefold(), payload.plan,
                    payload.subject, payload.category, payload.priority, "new", to_iso(now),
                    to_iso(now + timedelta(minutes=first_minutes)), to_iso(now + timedelta(minutes=resolution_minutes)), to_iso(now),
                ),
            )
            _append_message(conn, ticket_id=ticket_id, kind="customer", author=contact, body=payload.message, created_at=to_iso(now))
            _append_message(conn, ticket_id=ticket_id, kind="event", author=user["name"], body="Обращение создано в консоли Minima", created_at=to_iso(now))
            return _serialize_ticket(conn, ticket_id, viewer=user)

    @application.patch("/api/v1/support/tickets/{ticket_id}")
    def update_ticket(ticket_id: str, payload: TicketPatchBody, request: Request) -> dict:
        _validate_origin(request)
        admin = _require_admin(request, cfg)
        changes = payload.model_dump(exclude_unset=True)
        if not changes:
            raise HTTPException(status_code=422, detail="Не переданы поля для изменения.")
        now = utc_now()
        now_iso = to_iso(now)
        with connection(cfg.database_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Обращение не найдено.")

            updates: dict[str, str | None] = {}
            events: list[str] = []
            old_status = row["status"]
            old_priority = row["priority"]
            old_assigned = row["assigned"]

            if "priority" in changes and changes["priority"] is not None and changes["priority"] != old_priority:
                new_priority = changes["priority"]
                first_minutes, resolution_minutes = PRIORITIES[new_priority]
                created_at = _parse_iso(row["created_at"])
                updates["priority"] = new_priority
                updates["first_response_due"] = to_iso(created_at + timedelta(minutes=first_minutes))
                updates["resolution_due"] = to_iso(created_at + timedelta(minutes=resolution_minutes))
                events.append(f"Приоритет изменён: {PRIORITY_LABELS[new_priority]}")

            if "assigned" in changes:
                new_assigned = (changes["assigned"] or "").strip()
                if new_assigned != old_assigned:
                    updates["assigned"] = new_assigned
                    events.append(f"Назначено: {new_assigned}" if new_assigned else "Ответственный снят")

            if "status" in changes and changes["status"] is not None and changes["status"] != old_status:
                new_status = changes["status"]
                updates["status"] = new_status
                if new_status == "resolved":
                    updates["resolved_at"] = now_iso
                    events.append("Обращение закрыто")
                elif old_status == "resolved":
                    updates["resolved_at"] = None
                    events.append("Обращение переоткрыто")
                elif new_status == "waiting":
                    events.append("Ожидаем ответ клиента")
                elif old_status == "waiting" and new_status == "in_progress":
                    events.append("Обращение возвращено в работу")
                elif new_status == "in_progress":
                    events.append("Обращение взято в работу")

            effective_status = updates.get("status", old_status)
            if updates.get("assigned") and effective_status == "new" and "status" not in changes:
                updates["status"] = "in_progress"
                events.append("Обращение взято в работу")

            if not updates:
                return _serialize_ticket(conn, ticket_id, viewer=admin)
            updates["updated_at"] = now_iso
            assignments = ", ".join(f"{column} = ?" for column in updates)
            conn.execute(
                f"UPDATE tickets SET {assignments}, version = version + 1 WHERE id = ?",
                (*updates.values(), ticket_id),
            )
            for text in events:
                _append_message(conn, ticket_id=ticket_id, kind="event", author=admin["name"], body=text, created_at=now_iso)
            return _serialize_ticket(conn, ticket_id, viewer=admin)

    @application.post("/api/v1/support/tickets/{ticket_id}/messages")
    def create_message(
        ticket_id: str,
        payload: MessageCreateBody,
        request: Request,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=16, max_length=80)],
    ) -> dict:
        _validate_origin(request)
        admin = _require_admin(request, cfg)
        if not re.fullmatch(r"[A-Za-z0-9._:-]{16,80}", idempotency_key):
            raise HTTPException(status_code=422, detail="Некорректный ключ идемпотентности.")

        existing_id: int | None = None
        with connection(cfg.database_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Обращение не найдено.")
            existing = conn.execute("SELECT * FROM messages WHERE idempotency_key = ?", (idempotency_key,)).fetchone()
            if existing:
                same_request = (
                    existing["ticket_id"] == ticket_id
                    and existing["kind"] == payload.kind
                    and existing["body"] == payload.text
                )
                if not same_request:
                    raise HTTPException(status_code=409, detail="Этот ключ идемпотентности уже использован для другого сообщения.")
                if existing["delivery_status"] == "failed":
                    raise HTTPException(status_code=409, detail="Предыдущая отправка не удалась. Повторите отправку с новым сообщением.")
                if existing["delivery_status"] == "pending":
                    raise HTTPException(status_code=409, detail="Отправка этого сообщения уже выполняется.")
                return {"ticket": _serialize_ticket(conn, ticket_id, viewer=admin), "duplicate": True}
            if row["status"] == "resolved":
                raise HTTPException(status_code=409, detail="Переоткройте обращение перед отправкой сообщения.")
            if payload.kind == "support" and not cfg.smtp_configured:
                raise HTTPException(status_code=503, detail="SMTP не настроен. Заполните SMTP_HOST и SMTP_FROM в .env; письмо не отправлено.")
            delivery_status = "pending" if payload.kind == "support" else "not_applicable"
            existing_id = _append_message(
                conn,
                ticket_id=ticket_id,
                kind=payload.kind,
                author=admin["name"],
                body=payload.text,
                delivery_status=delivery_status,
                idempotency_key=idempotency_key,
            )

        if payload.kind == "note":
            with connection(cfg.database_path) as conn:
                return {"ticket": _serialize_ticket(conn, ticket_id, viewer=admin), "messageId": existing_id, "deliveryStatus": "not_applicable"}

        with connection(cfg.database_path) as conn:
            current = conn.execute("SELECT email, subject FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
        if not current:
            raise HTTPException(status_code=404, detail="Обращение не найдено.")
        try:
            send_support_email(
                cfg,
                recipient=current["email"],
                ticket_id=ticket_id,
                subject=current["subject"],
                body=payload.text,
            )
        except (MailNotConfigured, MailDeliveryError) as exc:
            with connection(cfg.database_path) as conn:
                conn.execute(
                    "UPDATE messages SET delivery_status = 'failed', provider_error = ? WHERE id = ?",
                    (type(exc).__name__, existing_id),
                )
            logger.warning("SMTP delivery failed for %s (%s)", ticket_id, type(exc).__name__)
            if isinstance(exc, MailNotConfigured):
                raise HTTPException(status_code=503, detail="SMTP не настроен. Письмо не отправлено.") from exc
            raise HTTPException(
                status_code=502,
                detail="Почтовый сервер не принял письмо. Проверьте настройки SMTP; сообщение отмечено как неотправленное.",
            ) from exc

        sent_at = iso_now()
        with connection(cfg.database_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "UPDATE messages SET delivery_status = 'sent' WHERE id = ?",
                (existing_id,),
            )
            ticket_row = conn.execute("SELECT status, first_responded_at FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
            updates: dict[str, str | None] = {"updated_at": sent_at}
            if not ticket_row["first_responded_at"]:
                updates["first_responded_at"] = sent_at
            if ticket_row["status"] in {"new", "waiting"}:
                updates["status"] = "in_progress"
            assignments = ", ".join(f"{column} = ?" for column in updates)
            conn.execute(
                f"UPDATE tickets SET {assignments}, version = version + 1 WHERE id = ?",
                (*updates.values(), ticket_id),
            )
            _append_message(conn, ticket_id=ticket_id, kind="event", author=admin["name"], body="Ответ передан почтовому серверу", created_at=sent_at)
            return {"ticket": _serialize_ticket(conn, ticket_id, viewer=admin), "messageId": existing_id, "deliveryStatus": "sent"}

    return application


app = create_app()
