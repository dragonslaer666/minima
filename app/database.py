from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator


PRIORITIES = {
    "urgent": (15, 4 * 60),
    "high": (60, 8 * 60),
    "normal": (4 * 60, 24 * 60),
    "low": (8 * 60, 72 * 60),
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS tickets (
    id TEXT PRIMARY KEY,
    company TEXT NOT NULL,
    contact TEXT NOT NULL,
    email TEXT NOT NULL,
    plan TEXT NOT NULL,
    subject TEXT NOT NULL,
    category TEXT NOT NULL,
    priority TEXT NOT NULL CHECK(priority IN ('urgent', 'high', 'normal', 'low')),
    status TEXT NOT NULL CHECK(status IN ('new', 'in_progress', 'waiting', 'resolved')),
    assigned TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    first_response_due TEXT NOT NULL,
    resolution_due TEXT NOT NULL,
    first_responded_at TEXT,
    resolved_at TEXT,
    updated_at TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_tickets_status_due ON tickets(status, first_response_due, resolution_due);
CREATE INDEX IF NOT EXISTS idx_tickets_updated ON tickets(updated_at DESC);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id TEXT NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK(kind IN ('customer', 'support', 'note', 'event')),
    author TEXT NOT NULL,
    body TEXT NOT NULL,
    created_at TEXT NOT NULL,
    delivery_status TEXT NOT NULL DEFAULT 'not_applicable'
      CHECK(delivery_status IN ('not_applicable', 'pending', 'sent', 'failed')),
    provider_error TEXT,
    idempotency_key TEXT UNIQUE
);
CREATE INDEX IF NOT EXISTS idx_messages_ticket ON messages(ticket_id, id);

CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    email TEXT NOT NULL,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_expiry ON sessions(expires_at);

CREATE TABLE IF NOT EXISTS login_throttles (
    ip_hash TEXT PRIMARY KEY,
    window_started_at TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    organization TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'owner' CHECK(role IN ('owner')),
    email_verified_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    last_login_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_users_role ON users(role);

CREATE TABLE IF NOT EXISTS email_verification_codes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT NOT NULL,
    purpose TEXT NOT NULL DEFAULT 'signup' CHECK(purpose IN ('signup')),
    code_hash TEXT NOT NULL,
    code_salt TEXT NOT NULL,
    draft_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    consumed_at TEXT,
    request_ip_hash TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_email_codes_lookup
    ON email_verification_codes(email, purpose, consumed_at, expires_at);

CREATE TABLE IF NOT EXISTS rate_limits (
    bucket TEXT NOT NULL,
    key_hash TEXT NOT NULL,
    window_started_at TEXT NOT NULL,
    window_seconds INTEGER NOT NULL,
    count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (bucket, key_hash)
);
"""

# Columns added to `sessions` after the first release; applied to existing databases.
SESSION_MIGRATIONS = {
    "role": "TEXT NOT NULL DEFAULT 'admin'",
    "organization": "TEXT NOT NULL DEFAULT ''",
    "user_id": "INTEGER",
}


@contextmanager
def connection(database_path: Path) -> Iterator[sqlite3.Connection]:
    database_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        if database_path.exists():
            os.chmod(database_path, 0o600)
        else:
            descriptor = os.open(database_path, os.O_CREAT | os.O_WRONLY, 0o600)
            os.close(descriptor)
    except OSError:
        # The SQLite connection below will surface a useful error if the path is unusable.
        pass
    conn = sqlite3.connect(str(database_path), timeout=15, isolation_level="DEFERRED")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 15000")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def to_iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def iso_now() -> str:
    return to_iso(utc_now()) or ""


def init_database(database_path: Path, seed_demo_data: bool = True) -> None:
    database_path.parent.mkdir(parents=True, exist_ok=True)
    with connection(database_path) as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)
        _migrate_sessions(conn)
        exists = conn.execute("SELECT 1 FROM tickets LIMIT 1").fetchone()
        if seed_demo_data and not exists:
            _seed_demo_data(conn)


def _migrate_sessions(conn: sqlite3.Connection) -> None:
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(sessions)").fetchall()}
    for column, ddl in SESSION_MIGRATIONS.items():
        if column not in existing:
            conn.execute(f"ALTER TABLE sessions ADD COLUMN {column} {ddl}")


def _seed_demo_data(conn: sqlite3.Connection) -> None:
    now = utc_now()
    # Reserved .example/.invalid domains make demo conversations safe: they cannot
    # accidentally reach real customer mailboxes if a user tests the send button.
    samples = [
        {
            "id": "SUP-4821", "company": "Вектор Маркет", "contact": "Мария Лебедева", "email": "m.lebedeva@vector-market.invalid", "plan": "Бизнес",
            "subject": "Не проходит оплата по счёту за май", "category": "Оплата и счета", "priority": "high", "status": "new", "assigned": "", "age": 48,
            "first_reply": None, "resolved_after": None,
            "messages": [("customer", "Мария Лебедева", "Здравствуйте! Пытаюсь оплатить счёт за май, но после подтверждения банка возвращает на страницу оплаты. В банке платёж отображается как отклонённый. Можете проверить, пожалуйста?", 0)],
        },
        {
            "id": "SUP-4824", "company": "Лавка у дома", "contact": "Сергей Мельников", "email": "s.melnikov@lavka-home.invalid", "plan": "Старт",
            "subject": "Сотрудник не может войти после смены телефона", "category": "Доступ и безопасность", "priority": "urgent", "status": "new", "assigned": "", "age": 13,
            "first_reply": None, "resolved_after": None,
            "messages": [("customer", "Сергей Мельников", "Сотрудник сменил телефон и теперь не может войти в Minima. Сброс приглашения не помог, а у нас прямо сейчас не обрабатываются новые обращения. Нужна помощь.", 0)],
        },
        {
            "id": "SUP-4822", "company": "Северный ветер", "contact": "Анна Громова", "email": "a.gromova@northwind.example", "plan": "Профи",
            "subject": "Как выгрузить задачи за прошлый квартал?", "category": "Настройка Minima", "priority": "normal", "status": "in_progress", "assigned": "Мария Волкова", "age": 132,
            "first_reply": 36, "resolved_after": None,
            "messages": [
                ("customer", "Анна Громова", "Подскажите, пожалуйста, можно ли выгрузить задачи за прошлый квартал вместе с комментариями?", 0),
                ("support", "Мария Волкова", "Здравствуйте, Анна! Проверяю доступные варианты экспорта и вернусь к вам с точными шагами.", 36),
            ],
        },
        {
            "id": "SUP-4818", "company": "Кофейня «Точка»", "contact": "Егор Сафонов", "email": "e.safonov@tochka.example", "plan": "Бизнес",
            "subject": "Нужно подключить второй Telegram-бот", "category": "Интеграции", "priority": "high", "status": "waiting", "assigned": "Илья Соколов", "age": 305,
            "first_reply": 18, "resolved_after": None,
            "messages": [
                ("customer", "Егор Сафонов", "Хотим подключить отдельного бота для филиала. Можно ли добавить его к нашей организации?", 0),
                ("support", "Илья Соколов", "Да, это возможно. Отправил короткую инструкцию по настройке webhook и проверке прав бота.", 18),
                ("customer", "Егор Сафонов", "Спасибо! Попробуем после закрытия. Если что, напишу.", 181),
            ],
        },
        {
            "id": "SUP-4815", "company": "Клиника «Маяк»", "contact": "Ольга Тимофеева", "email": "o.timofeeva@mayak.invalid", "plan": "Энтерпрайз",
            "subject": "Счёт выставлен на старые реквизиты", "category": "Оплата и счета", "priority": "normal", "status": "in_progress", "assigned": "Алина Кузнецова", "age": 1060,
            "first_reply": 39, "resolved_after": None,
            "messages": [
                ("customer", "Ольга Тимофеева", "В новом счёте указаны старые банковские реквизиты. Мы уже отправляли обновлённые документы на прошлой неделе.", 0),
                ("support", "Алина Кузнецова", "Спасибо, Ольга. Передала запрос в биллинг, проверяем данные организации. Вернусь с обновлённым счётом.", 39),
                ("note", "Алексей Крылов", "Проверить подтверждение реквизитов в финансовом журнале перед переизданием.", 80),
            ],
        },
        {
            "id": "SUP-4807", "company": "ТехноСклад", "contact": "Роман Ким", "email": "r.kim@tehnosklad.example", "plan": "Профи",
            "subject": "Вопрос по лимиту AI-разборов", "category": "Настройка Minima", "priority": "low", "status": "resolved", "assigned": "Денис Орлов", "age": 4320,
            "first_reply": 48, "resolved_after": 95,
            "messages": [
                ("customer", "Роман Ким", "Подскажите, когда обновится месячный лимит AI-разборов?", 0),
                ("support", "Денис Орлов", "Лимит обновляется в дату начала подписки. Проверил ваш аккаунт — следующий сброс будет 1 числа.", 48),
                ("event", "Алексей Крылов", "Обращение закрыто", 95),
            ],
        },
    ]
    for item in samples:
        created = now - timedelta(minutes=item["age"])
        first_minutes, resolution_minutes = PRIORITIES[item["priority"]]
        first_at = created + timedelta(minutes=item["first_reply"]) if item["first_reply"] is not None else None
        resolved_at = created + timedelta(minutes=item["resolved_after"]) if item["resolved_after"] is not None else None
        conn.execute(
            """INSERT INTO tickets (
              id, company, contact, email, plan, subject, category, priority, status, assigned,
              created_at, first_response_due, resolution_due, first_responded_at, resolved_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                item["id"], item["company"], item["contact"], item["email"], item["plan"], item["subject"], item["category"],
                item["priority"], item["status"], item["assigned"], to_iso(created),
                to_iso(created + timedelta(minutes=first_minutes)), to_iso(created + timedelta(minutes=resolution_minutes)),
                to_iso(first_at), to_iso(resolved_at), to_iso(now),
            ),
        )
        for kind, author, body, minutes_after_creation in item["messages"]:
            conn.execute(
                "INSERT INTO messages (ticket_id, kind, author, body, created_at, delivery_status) VALUES (?, ?, ?, ?, ?, 'not_applicable')",
                (item["id"], kind, author, body, to_iso(created + timedelta(minutes=minutes_after_creation))),
            )
