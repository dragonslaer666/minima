"""Registration of an organization owner with email confirmation."""
from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.database import to_iso, utc_now
from app.main import create_app
from app.mailer import MailDeliveryError

OWNER_EMAIL = "owner@vector-market.invalid"
OWNER_PASSWORD = "Strong-owner-pass-2026"


class CodeCapture:
    """Replaces the SMTP sender and remembers every code the backend tried to send."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def __call__(self, settings, **kwargs):
        self.calls.append(kwargs)

    @property
    def last_code(self) -> str:
        return str(self.calls[-1]["code"])


def settings_for(tmp_path: Path, *, smtp: bool = True, **overrides) -> Settings:
    base = dict(
        app_env="test",
        database_path=tmp_path / "signup.sqlite3",
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
        signup_enabled=True,
        password_min_length=12,
        email_code_ttl_minutes=15,
        email_code_length=6,
        email_code_max_attempts=5,
        email_code_resend_cooldown_seconds=60,
        email_code_max_per_email_hour=5,
        email_code_max_per_ip_hour=20,
        confirm_max_per_ip_hour=30,
        dev_log_email_codes=False,
    )
    base.update(overrides)
    return Settings(**base)


def make_client(cfg: Settings):
    app = create_app(cfg)
    return TestClient(app)


def users(database_path: Path) -> list[sqlite3.Row]:
    with sqlite3.connect(database_path) as conn:
        conn.row_factory = sqlite3.Row
        return list(conn.execute("SELECT * FROM users").fetchall())


def active_codes(database_path: Path, email: str) -> list[sqlite3.Row]:
    with sqlite3.connect(database_path) as conn:
        conn.row_factory = sqlite3.Row
        return list(
            conn.execute(
                "SELECT * FROM email_verification_codes WHERE email = ? AND consumed_at IS NULL",
                (email,),
            ).fetchall()
        )


def shift_code_time(database_path: Path, email: str, *, column: str, delta: timedelta) -> None:
    now = utc_now() + delta
    with sqlite3.connect(database_path) as conn:
        conn.execute(
            f"UPDATE email_verification_codes SET {column} = ? WHERE email = ?",
            (to_iso(now), email.casefold()),
        )
        conn.commit()


def request_code(client: TestClient, *, email: str = OWNER_EMAIL, organization: str = "Вектор Маркет",
                 name: str = "Мария Лебедева", password: str = OWNER_PASSWORD):
    return client.post(
        "/api/v1/auth/register/request",
        json={"organization": organization, "name": name, "email": email, "password": password},
    )


@pytest.fixture
def owner_setup(tmp_path, monkeypatch):
    cfg = settings_for(tmp_path)
    with make_client(cfg) as client:
        capture = CodeCapture()
        monkeypatch.setattr("app.main.send_verification_email", capture)
        yield cfg, client, capture


def test_owner_registration_confirms_email_and_creates_account(owner_setup):
    cfg, client, capture = owner_setup
    response = request_code(client)
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["status"] == "sent"
    assert body["delivery"] == "email"
    assert body["email"] == OWNER_EMAIL
    assert body["expiresInSeconds"] == 900
    assert body["codeLength"] == 6
    assert body["resendAvailableInSeconds"] == 60
    assert capture.calls[-1]["recipient"] == OWNER_EMAIL
    assert capture.calls[-1]["code"] == capture.last_code
    assert users(cfg.database_path) == []

    wrong = client.post("/api/v1/auth/register/confirm", json={"email": OWNER_EMAIL, "code": "000000"})
    assert wrong.status_code == 400
    assert "Осталось попыток: 4" in wrong.json()["detail"]
    assert users(cfg.database_path) == []

    confirmed = client.post("/api/v1/auth/register/confirm", json={"email": OWNER_EMAIL, "code": capture.last_code})
    assert confirmed.status_code == 201, confirmed.text
    assert confirmed.json()["authenticated"] is True
    assert confirmed.json()["role"] == "owner"
    assert confirmed.json()["user"] == {
        "email": OWNER_EMAIL,
        "name": "Мария Лебедева",
        "role": "owner",
        "organization": "Вектор Маркет",
    }
    assert client.cookies.get("minima_test_session")

    stored = users(cfg.database_path)
    assert len(stored) == 1
    assert stored[0]["email"] == OWNER_EMAIL
    assert stored[0]["email_verified_at"]
    assert OWNER_PASSWORD not in stored[0]["password_hash"]
    assert "pbkdf2_sha256$" in stored[0]["password_hash"]

    session = client.get("/api/v1/auth/session").json()
    assert session["authenticated"] is True
    assert session["role"] == "owner"
    assert session["admin"] is None
    assert session["organization"] == "Вектор Маркет"


def test_code_is_single_use_and_stored_hashed(owner_setup):
    cfg, client, capture = owner_setup
    request_code(client)
    code = capture.last_code
    row = active_codes(cfg.database_path, OWNER_EMAIL)[0]
    assert code not in row["code_hash"]
    assert row["draft_json"]
    assert OWNER_PASSWORD not in row["draft_json"]

    assert client.post("/api/v1/auth/register/confirm", json={"email": OWNER_EMAIL, "code": code}).status_code == 201
    assert active_codes(cfg.database_path, OWNER_EMAIL) == []
    second = client.post("/api/v1/auth/register/confirm", json={"email": OWNER_EMAIL, "code": code})
    assert second.status_code == 400
    assert len(users(cfg.database_path)) == 1


def test_owner_can_log_in_with_password_after_confirmation(owner_setup):
    cfg, client, capture = owner_setup
    request_code(client)
    client.post("/api/v1/auth/register/confirm", json={"email": OWNER_EMAIL, "code": capture.last_code})
    client.post("/api/v1/auth/logout")

    rejected = client.post("/api/v1/auth/login", json={"email": OWNER_EMAIL, "password": "wrong-password-1"})
    assert rejected.status_code == 401
    accepted = client.post("/api/v1/auth/login", json={"email": OWNER_EMAIL, "password": OWNER_PASSWORD})
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["role"] == "owner"
    assert accepted.json()["admin"] is None
    assert accepted.json()["user"]["organization"] == "Вектор Маркет"

    stored = users(cfg.database_path)[0]
    assert stored["last_login_at"]


def test_expired_code_is_rejected_and_creates_nothing(owner_setup):
    cfg, client, capture = owner_setup
    request_code(client)
    shift_code_time(cfg.database_path, OWNER_EMAIL, column="expires_at", delta=-timedelta(minutes=1))
    response = client.post("/api/v1/auth/register/confirm", json={"email": OWNER_EMAIL, "code": capture.last_code})
    assert response.status_code == 410
    assert "истёк" in response.json()["detail"]
    assert users(cfg.database_path) == []
    assert active_codes(cfg.database_path, OWNER_EMAIL) == []


def test_attempt_limit_locks_the_code(owner_setup):
    cfg, client, capture = owner_setup
    request_code(client)
    code = capture.last_code
    for remaining in (4, 3, 2, 1, 0):
        response = client.post("/api/v1/auth/register/confirm", json={"email": OWNER_EMAIL, "code": "111111"})
        if remaining:
            assert response.status_code == 400
            assert f"Осталось попыток: {remaining}" in response.json()["detail"]
        else:
            assert response.status_code == 429
    # The correct code no longer helps: the code is spent and nothing was created.
    assert client.post("/api/v1/auth/register/confirm", json={"email": OWNER_EMAIL, "code": code}).status_code == 400
    assert users(cfg.database_path) == []


def test_resend_replaces_code_and_respects_cooldown(owner_setup):
    cfg, client, capture = owner_setup
    request_code(client)
    first_code = capture.last_code

    too_soon = client.post("/api/v1/auth/register/resend", json={"email": OWNER_EMAIL})
    assert too_soon.status_code == 429
    assert too_soon.headers.get("retry-after")
    assert len(capture.calls) == 1

    shift_code_time(cfg.database_path, OWNER_EMAIL, column="created_at", delta=-timedelta(minutes=5))
    resent = client.post("/api/v1/auth/register/resend", json={"email": OWNER_EMAIL})
    assert resent.status_code == 202, resent.text
    second_code = capture.last_code
    assert len(capture.calls) == 2

    assert client.post("/api/v1/auth/register/confirm", json={"email": OWNER_EMAIL, "code": first_code}).status_code == 400
    assert client.post("/api/v1/auth/register/confirm", json={"email": OWNER_EMAIL, "code": second_code}).status_code == 201
    assert users(cfg.database_path)[0]["email"] == OWNER_EMAIL


def test_code_requests_are_rate_limited_per_email(tmp_path, monkeypatch):
    cfg = settings_for(tmp_path, email_code_max_per_email_hour=1)
    with make_client(cfg) as client:
        capture = CodeCapture()
        monkeypatch.setattr("app.main.send_verification_email", capture)
        assert request_code(client).status_code == 202
        limited = request_code(client)
        assert limited.status_code == 429
        assert "почты" in limited.json()["detail"]
        assert limited.headers.get("retry-after")
        assert len(capture.calls) == 1


def test_registration_without_smtp_does_not_send_or_create(tmp_path, monkeypatch):
    cfg = settings_for(tmp_path, smtp=False)
    with make_client(cfg) as client:
        capture = CodeCapture()
        monkeypatch.setattr("app.main.send_verification_email", capture)
        response = request_code(client)
        assert response.status_code == 503
        assert "учётная запись не создана" in response.json()["detail"]
        assert capture.calls == []
        assert active_codes(cfg.database_path, OWNER_EMAIL) == []
        assert users(cfg.database_path) == []
        session = client.get("/api/v1/auth/session").json()
        assert session["smtpConfigured"] is False


def test_dev_log_mode_keeps_registration_offline(tmp_path, monkeypatch, caplog):
    cfg = settings_for(tmp_path, smtp=False, dev_log_email_codes=True)
    with make_client(cfg) as client, caplog.at_level("WARNING", logger="minima.support"):
        response = request_code(client)
        assert response.status_code == 202
        body = response.json()
        assert body["status"] == "logged"
        assert body["delivery"] == "server-log"
        assert body["message"].lower().count("не отправлено") >= 1
        logged = caplog.records[-1].getMessage()
        assert OWNER_EMAIL in logged
        assert "НЕ отправлено" in logged
        assert client.post("/api/v1/auth/register/confirm", json={"email": OWNER_EMAIL, "code": "000000"}).status_code == 400
        code = re.search(r"\b\d{6}\b", logged).group(0)
        assert client.post("/api/v1/auth/register/confirm", json={"email": OWNER_EMAIL, "code": code}).status_code == 201
        assert users(cfg.database_path)[0]["email"] == OWNER_EMAIL


def test_smtp_failure_returns_502_without_a_usable_code(tmp_path, monkeypatch):
    cfg = settings_for(tmp_path)
    with make_client(cfg) as client:
        def fail(*_args, **_kwargs):
            raise MailDeliveryError("SMTPConnectError")

        monkeypatch.setattr("app.main.send_verification_email", fail)
        response = request_code(client)
        assert response.status_code == 502
        assert active_codes(cfg.database_path, OWNER_EMAIL) == []
        assert users(cfg.database_path) == []


def test_weak_password_and_bad_email_are_rejected(owner_setup):
    _cfg, client, capture = owner_setup
    weak = request_code(client, password="short1")
    assert weak.status_code == 422
    no_digits = request_code(client, password="PasswordWithoutDigits")
    assert no_digits.status_code == 422
    bad_email = client.post(
        "/api/v1/auth/register/request",
        json={"organization": "Орг", "name": "Имя", "email": "not-an-email", "password": OWNER_PASSWORD},
    )
    assert bad_email.status_code == 422
    assert capture.calls == []


def test_disabled_signup_hides_registration(tmp_path):
    cfg = settings_for(tmp_path, signup_enabled=False)
    with make_client(cfg) as client:
        assert client.get("/api/v1/auth/session").json()["signupEnabled"] is False
        assert request_code(client).status_code == 404
        assert client.post("/api/v1/auth/register/resend", json={"email": OWNER_EMAIL}).status_code == 404
        assert client.post("/api/v1/auth/register/confirm", json={"email": OWNER_EMAIL, "code": "123456"}).status_code == 404


def test_cross_origin_registration_is_rejected(owner_setup):
    _cfg, client, _capture = owner_setup
    response = client.post(
        "/api/v1/auth/register/request",
        headers={"Origin": "https://attacker.invalid"},
        json={"organization": "Орг", "name": "Имя", "email": OWNER_EMAIL, "password": OWNER_PASSWORD},
    )
    assert response.status_code == 403


def test_owner_sees_only_own_organization_and_no_internal_notes(tmp_path, monkeypatch):
    cfg = settings_for(tmp_path)
    with make_client(cfg) as client:
        capture = CodeCapture()
        monkeypatch.setattr("app.main.send_verification_email", capture)
        request_code(client, email="clinic@mayak.invalid", organization="Клиника «Маяк»", name="Ольга Тимофеева")
        assert client.post(
            "/api/v1/auth/register/confirm", json={"email": "clinic@mayak.invalid", "code": capture.last_code}
        ).status_code == 201

        queue = client.get("/api/v1/support/tickets").json()["tickets"]
        assert [ticket["id"] for ticket in queue] == ["SUP-4815"]
        assert queue[0]["company"] == "Клиника «Маяк»"
        assert all(message["kind"] != "note" for message in queue[0]["messages"])

        forbidden_patch = client.patch("/api/v1/support/tickets/SUP-4815", json={"status": "resolved"})
        assert forbidden_patch.status_code == 403
        forbidden_reply = client.post(
            "/api/v1/support/tickets/SUP-4815/messages",
            headers={"Idempotency-Key": "owner-reply-0000001"},
            json={"kind": "support", "text": "Ответ владельца не должен уйти клиенту."},
        )
        assert forbidden_reply.status_code == 403
        hidden = client.get("/api/v1/support/tickets/SUP-4821")
        assert hidden.status_code == 404

        created = client.post(
            "/api/v1/support/tickets",
            json={
                "company": "Чужая организация",
                "contact": "Ольга Тимофеева",
                "email": "clinic@mayak.invalid",
                "subject": "Не открывается отчёт по филиалу",
                "priority": "high",
                "message": "Прошу проверить отчёт по филиалу.",
            },
        )
        assert created.status_code == 201, created.text
        assert created.json()["company"] == "Клиника «Маяк»"


def test_admin_still_sees_every_organization(owner_setup):
    cfg, client, capture = owner_setup
    request_code(client)
    client.post("/api/v1/auth/register/confirm", json={"email": OWNER_EMAIL, "code": capture.last_code})
    client.post("/api/v1/auth/logout")

    client.post("/api/v1/auth/login", json={"email": "admin@minima.test", "password": "Strong-test-password-2026"})
    session = client.get("/api/v1/auth/session").json()
    assert session["role"] == "admin"
    assert session["admin"] == {"email": "admin@minima.test", "name": "Тестовый администратор"}
    queue = client.get("/api/v1/support/tickets").json()["tickets"]
    assert len(queue) == 6
    assert "SUP-4815" in [ticket["id"] for ticket in queue]
    assert client.get("/api/v1/support/tickets/SUP-4815").json()["messages"]
