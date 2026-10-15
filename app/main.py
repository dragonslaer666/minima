from __future__ import annotations

import hashlib
import hmac
import logging
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
from app.mailer import MailDeliveryError, MailNotConfigured, send_support_email

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


class LoginBody(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=256)


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


def _admin_from_request(request: Request, cfg: Settings) -> dict[str, str] | None:
    token = request.cookies.get(cfg.session_cookie)
    if not token:
        return None
    with connection(cfg.database_path) as conn:
        row = conn.execute(
            "SELECT email, name, expires_at FROM sessions WHERE token_hash = ?",
            (_session_hash(token, cfg),),
        ).fetchone()
        if not row:
            return None
        if row["expires_at"] <= iso_now():
            conn.execute("DELETE FROM sessions WHERE token_hash = ?", (_session_hash(token, cfg),))
            return None
        return {"email": row["email"], "name": row["name"]}


def _require_admin(request: Request, cfg: Settings) -> dict[str, str]:
    admin = _admin_from_request(request, cfg)
    if not admin:
        raise HTTPException(status_code=401, detail="Требуется войти в консоль администратора.")
    return admin


def _serialize_ticket(conn: sqlite3.Connection, ticket_id: str) -> dict:
    ticket = conn.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
    if not ticket:
        raise HTTPException(status_code=404, detail="Обращение не найдено.")
    messages = conn.execute(
        "SELECT id, kind, author, body, created_at, delivery_status FROM messages WHERE ticket_id = ? ORDER BY id",
        (ticket_id,),
    ).fetchall()
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
            for item in messages
        ],
    }


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


def create_app(settings: Settings | None = None) -> FastAPI:
    cfg = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        cfg.validate()
        init_database(cfg.database_path, cfg.seed_demo_data)
        yield

    application = FastAPI(
        title="Minima Support API",
        version="1.0.0",
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
        admin = _admin_from_request(request, cfg)
        return {"authenticated": bool(admin), "admin": admin, "smtpConfigured": cfg.smtp_configured}

    @application.post("/api/v1/auth/login")
    def login(payload: LoginBody, request: Request, response: Response) -> dict:
        _validate_origin(request)
        email_ok = hmac.compare_digest(payload.email.casefold(), cfg.admin_email.casefold())
        password_ok = hmac.compare_digest(payload.password, cfg.admin_password)
        credentials_ok = email_ok and password_ok
        client_ip = request.client.host if request.client else "unknown"
        ip_hash = hmac.new(cfg.session_secret.encode("utf-8"), f"login:{client_ip}".encode("utf-8"), hashlib.sha256).hexdigest()
        now = utc_now()
        now_iso = to_iso(now)
        throttle_seconds = 15 * 60
        blocked_for = 0
        invalid_credentials = False
        token = secrets.token_urlsafe(36) if credentials_ok else ""
        expiry = now + timedelta(hours=cfg.session_ttl_hours)
        with connection(cfg.database_path) as conn:
            throttle = conn.execute("SELECT window_started_at, attempts FROM login_throttles WHERE ip_hash = ?", (ip_hash,)).fetchone()
            if throttle:
                elapsed = (now - _parse_iso(throttle["window_started_at"])).total_seconds()
                if elapsed < throttle_seconds and throttle["attempts"] >= 5:
                    blocked_for = max(1, int(throttle_seconds - elapsed))
                elif not credentials_ok and elapsed < throttle_seconds:
                    conn.execute("UPDATE login_throttles SET attempts = attempts + 1 WHERE ip_hash = ?", (ip_hash,))
                    invalid_credentials = True
                elif not credentials_ok:
                    conn.execute("UPDATE login_throttles SET window_started_at = ?, attempts = 1 WHERE ip_hash = ?", (now_iso, ip_hash))
                    invalid_credentials = True
                else:
                    conn.execute("DELETE FROM login_throttles WHERE ip_hash = ?", (ip_hash,))
            elif not credentials_ok:
                conn.execute("INSERT INTO login_throttles (ip_hash, window_started_at, attempts) VALUES (?, ?, 1)", (ip_hash, now_iso))
                invalid_credentials = True

            if credentials_ok and not blocked_for:
                conn.execute("DELETE FROM sessions WHERE expires_at <= ?", (now_iso,))
                conn.execute(
                    "INSERT INTO sessions (token_hash, email, name, created_at, expires_at) VALUES (?, ?, ?, ?, ?)",
                    (_session_hash(token, cfg), cfg.admin_email.casefold(), cfg.admin_name, now_iso, to_iso(expiry)),
                )
        if blocked_for:
            raise HTTPException(status_code=429, detail="Слишком много попыток входа. Попробуйте позже.", headers={"Retry-After": str(blocked_for)})
        if invalid_credentials:
            raise HTTPException(status_code=401, detail="Неверная почта или пароль.")
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
        return {"authenticated": True, "admin": {"email": cfg.admin_email, "name": cfg.admin_name}, "smtpConfigured": cfg.smtp_configured}

    @application.post("/api/v1/auth/logout")
    def logout(request: Request, response: Response) -> dict[str, bool]:
        _validate_origin(request)
        token = request.cookies.get(cfg.session_cookie)
        if token:
            with connection(cfg.database_path) as conn:
                conn.execute("DELETE FROM sessions WHERE token_hash = ?", (_session_hash(token, cfg),))
        response.delete_cookie(cfg.session_cookie, path="/", httponly=True, secure=cfg.cookie_secure, samesite="strict")
        return {"ok": True}

    @application.get("/api/v1/support/tickets")
    def tickets(request: Request) -> dict:
        _require_admin(request, cfg)
        with connection(cfg.database_path) as conn:
            rows = conn.execute(
                """SELECT id FROM tickets
                   ORDER BY CASE WHEN status = 'resolved' THEN 1 ELSE 0 END,
                     CASE WHEN first_responded_at IS NULL THEN first_response_due ELSE resolution_due END,
                     created_at DESC"""
            ).fetchall()
            return {"tickets": [_serialize_ticket(conn, row["id"]) for row in rows]}

    @application.get("/api/v1/support/tickets/{ticket_id}")
    def ticket(ticket_id: str, request: Request) -> dict:
        _require_admin(request, cfg)
        with connection(cfg.database_path) as conn:
            return _serialize_ticket(conn, ticket_id)

    @application.post("/api/v1/support/tickets", status_code=status.HTTP_201_CREATED)
    def create_ticket(payload: TicketCreateBody, request: Request) -> dict:
        _validate_origin(request)
        admin = _require_admin(request, cfg)
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
                    ticket_id, payload.company, payload.contact, payload.email.casefold(), payload.plan,
                    payload.subject, payload.category, payload.priority, "new", to_iso(now),
                    to_iso(now + timedelta(minutes=first_minutes)), to_iso(now + timedelta(minutes=resolution_minutes)), to_iso(now),
                ),
            )
            _append_message(conn, ticket_id=ticket_id, kind="customer", author=payload.contact, body=payload.message, created_at=to_iso(now))
            _append_message(conn, ticket_id=ticket_id, kind="event", author=admin["name"], body="Обращение создано оператором поддержки", created_at=to_iso(now))
            return _serialize_ticket(conn, ticket_id)

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
                return _serialize_ticket(conn, ticket_id)
            updates["updated_at"] = now_iso
            assignments = ", ".join(f"{column} = ?" for column in updates)
            conn.execute(
                f"UPDATE tickets SET {assignments}, version = version + 1 WHERE id = ?",
                (*updates.values(), ticket_id),
            )
            for text in events:
                _append_message(conn, ticket_id=ticket_id, kind="event", author=admin["name"], body=text, created_at=now_iso)
            return _serialize_ticket(conn, ticket_id)

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
                return {"ticket": _serialize_ticket(conn, ticket_id), "duplicate": True}
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
                return {"ticket": _serialize_ticket(conn, ticket_id), "messageId": existing_id, "deliveryStatus": "not_applicable"}

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
            return {"ticket": _serialize_ticket(conn, ticket_id), "messageId": existing_id, "deliveryStatus": "sent"}

    return application


app = create_app()
