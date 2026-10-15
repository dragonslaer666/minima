from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.mailer import MailDeliveryError


class MailCapture:
    def __init__(self):
        self.calls = []

    def __call__(self, settings, **kwargs):
        self.calls.append(kwargs)


def settings_for(tmp_path: Path, *, smtp: bool = True) -> Settings:
    return Settings(
        app_env="test",
        database_path=tmp_path / "support.sqlite3",
        seed_demo_data=True,
        admin_email="admin@minima.test",
        admin_name="Тестовый администратор",
        admin_password="Strong-test-password-2026",
        session_secret="test-session-secret-" + "x" * 48,
        session_cookie="minima_test_session",
        session_ttl_hours=8,
        cookie_secure=False,
        smtp_host="smtp.test.invalid" if smtp else "",
        smtp_port=587,
        smtp_username="smtp-user" if smtp else "",
        smtp_password="smtp-password" if smtp else "",
        smtp_from="Minima Support <support@minima.invalid>" if smtp else "",
        smtp_reply_to="reply@minima.invalid" if smtp else "",
        smtp_starttls=True,
        smtp_ssl=False,
        smtp_timeout=1,
    )


@pytest.fixture

def client(tmp_path, monkeypatch):
    cfg = settings_for(tmp_path)
    app = create_app(cfg)
    with TestClient(app) as test_client:
        yield test_client


def login(client: TestClient) -> None:
    response = client.post("/api/v1/auth/login", json={"email": "admin@minima.test", "password": "Strong-test-password-2026"})
    assert response.status_code == 200, response.text
    assert response.json()["authenticated"] is True


def test_auth_session_and_ticket_list(client: TestClient):
    assert client.get("/api/v1/auth/session").json() == {
        "authenticated": False,
        "admin": None,
        "smtpConfigured": True,
    }
    assert client.get("/api/v1/support/tickets").status_code == 401
    assert client.post("/api/v1/auth/login", json={"email": "admin@minima.test", "password": "incorrect"}).status_code == 401

    login(client)
    session = client.get("/api/v1/auth/session").json()
    assert session["authenticated"] is True
    assert session["admin"] == {"email": "admin@minima.test", "name": "Тестовый администратор"}
    assert "password" not in session

    result = client.get("/api/v1/support/tickets")
    assert result.status_code == 200
    tickets = result.json()["tickets"]
    assert len(tickets) == 6
    assert all(ticket["messages"] for ticket in tickets)
    assert all(ticket["email"].endswith((".invalid", ".example")) for ticket in tickets)


def test_create_and_update_persist_ticket_and_sla(client: TestClient):
    login(client)
    response = client.post(
        "/api/v1/support/tickets",
        json={
            "company": "Тестовая организация",
            "contact": "Анна Проверкина",
            "email": "anna@example.invalid",
            "plan": "Бизнес",
            "subject": "Не загружается отчёт",
            "category": "Настройка Minima",
            "priority": "normal",
            "message": "При открытии отчёта получаю пустую страницу.",
        },
    )
    assert response.status_code == 201, response.text
    ticket = response.json()
    assert ticket["id"].startswith("SUP-")
    assert ticket["status"] == "new"
    assert len(ticket["messages"]) == 2
    assert ticket["messages"][0]["kind"] == "customer"
    old_first_due = datetime.fromisoformat(ticket["firstResponseDue"].replace("Z", "+00:00"))

    changed = client.patch(f"/api/v1/support/tickets/{ticket['id']}", json={"priority": "urgent"})
    assert changed.status_code == 200, changed.text
    assert changed.json()["priority"] == "urgent"
    new_first_due = datetime.fromisoformat(changed.json()["firstResponseDue"].replace("Z", "+00:00"))
    assert (new_first_due - old_first_due).total_seconds() == -225 * 60

    assigned = client.patch(f"/api/v1/support/tickets/{ticket['id']}", json={"assigned": "Мария Волкова"})
    assert assigned.json()["assigned"] == "Мария Волкова"
    assert assigned.json()["status"] == "in_progress"
    assert any(message["kind"] == "event" and "Назначено" in message["text"] for message in assigned.json()["messages"])

    closed = client.patch(f"/api/v1/support/tickets/{ticket['id']}", json={"status": "resolved"})
    assert closed.json()["resolvedAt"]
    reopened = client.patch(f"/api/v1/support/tickets/{ticket['id']}", json={"status": "in_progress"})
    assert reopened.json()["resolvedAt"] is None
    assert reopened.json()["status"] == "in_progress"


def test_public_reply_sends_email_once_and_notes_stay_internal(client: TestClient, monkeypatch):
    from app import main

    capture = MailCapture()
    monkeypatch.setattr(main, "send_support_email", capture)
    login(client)
    ticket_id = "SUP-4821"

    note_key = "note-key-00000001"
    note = client.post(
        f"/api/v1/support/tickets/{ticket_id}/messages",
        headers={"Idempotency-Key": note_key},
        json={"kind": "note", "text": "Проверить счёт в тестовом стенде."},
    )
    assert note.status_code == 200, note.text
    assert note.json()["deliveryStatus"] == "not_applicable"
    assert capture.calls == []
    assert any(m["kind"] == "note" for m in note.json()["ticket"]["messages"])

    message_key = "reply-key-00000001"
    response = client.post(
        f"/api/v1/support/tickets/{ticket_id}/messages",
        headers={"Idempotency-Key": message_key},
        json={"kind": "support", "text": "Здравствуйте! Проверяем платёж и скоро вернёмся с ответом."},
    )
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["deliveryStatus"] == "sent"
    assert result["ticket"]["firstRespondedAt"]
    assert result["ticket"]["status"] == "in_progress"
    assert capture.calls == [{
        "recipient": "m.lebedeva@vector-market.invalid",
        "ticket_id": ticket_id,
        "subject": "Не проходит оплата по счёту за май",
        "body": "Здравствуйте! Проверяем платёж и скоро вернёмся с ответом.",
    }]

    duplicate = client.post(
        f"/api/v1/support/tickets/{ticket_id}/messages",
        headers={"Idempotency-Key": message_key},
        json={"kind": "support", "text": "Здравствуйте! Проверяем платёж и скоро вернёмся с ответом."},
    )
    assert duplicate.status_code == 200
    assert duplicate.json()["duplicate"] is True
    assert len(capture.calls) == 1

    conflict = client.post(
        f"/api/v1/support/tickets/{ticket_id}/messages",
        headers={"Idempotency-Key": message_key},
        json={"kind": "support", "text": "Другой текст"},
    )
    assert conflict.status_code == 409


def test_smtp_failure_is_recorded_and_does_not_stop_first_response_sla(client: TestClient, monkeypatch):
    from app import main

    def fail_send(*_args, **_kwargs):
        raise MailDeliveryError("SMTPConnectError")

    monkeypatch.setattr(main, "send_support_email", fail_send)
    login(client)
    response = client.post(
        "/api/v1/support/tickets/SUP-4821/messages",
        headers={"Idempotency-Key": "failed-reply-000001"},
        json={"kind": "support", "text": "Тестируем контролируемую ошибку SMTP."},
    )
    assert response.status_code == 502
    ticket = client.get("/api/v1/support/tickets/SUP-4821").json()
    failed = [message for message in ticket["messages"] if message["text"] == "Тестируем контролируемую ошибку SMTP."]
    assert len(failed) == 1
    assert failed[0]["deliveryStatus"] == "failed"
    assert ticket["firstRespondedAt"] is None
    assert ticket["status"] == "new"


def test_smtp_must_be_configured_for_public_reply_but_not_for_internal_note(tmp_path):
    cfg = settings_for(tmp_path, smtp=False)
    app = create_app(cfg)
    with TestClient(app) as client:
        login(client)
        session = client.get("/api/v1/auth/session").json()
        assert session["smtpConfigured"] is False
        note = client.post(
            "/api/v1/support/tickets/SUP-4821/messages",
            headers={"Idempotency-Key": "local-note-0000001"},
            json={"kind": "note", "text": "Внутренняя заметка остаётся доступной."},
        )
        assert note.status_code == 200
        response = client.post(
            "/api/v1/support/tickets/SUP-4821/messages",
            headers={"Idempotency-Key": "local-reply-0000001"},
            json={"kind": "support", "text": "Это письмо не должно быть отправлено."},
        )
        assert response.status_code == 503
        assert client.get("/api/v1/support/tickets/SUP-4821").json()["firstRespondedAt"] is None


def test_origin_check_and_logout(client: TestClient):
    denied = client.post(
        "/api/v1/auth/login",
        headers={"Origin": "https://attacker.invalid"},
        json={"email": "admin@minima.test", "password": "Strong-test-password-2026"},
    )
    assert denied.status_code == 403
    login(client)
    assert client.post("/api/v1/auth/logout").status_code == 200
    assert client.get("/api/v1/auth/session").json()["authenticated"] is False
    assert client.get("/api/v1/support/tickets").status_code == 401


def test_login_attempts_are_rate_limited(client: TestClient):
    for _ in range(5):
        response = client.post("/api/v1/auth/login", json={"email": "admin@minima.test", "password": "incorrect"})
        assert response.status_code == 401
    blocked = client.post("/api/v1/auth/login", json={"email": "admin@minima.test", "password": "incorrect"})
    assert blocked.status_code == 429
    assert blocked.headers.get("retry-after")
