from __future__ import annotations

import smtplib
import ssl
from email.message import EmailMessage
from email.utils import parseaddr

from app.config import Settings


class MailNotConfigured(RuntimeError):
    pass


class MailDeliveryError(RuntimeError):
    """A sanitized exception that is safe to return to the operator."""


def send_support_email(settings: Settings, *, recipient: str, ticket_id: str, subject: str, body: str) -> None:
    if not settings.smtp_configured:
        raise MailNotConfigured("SMTP is not configured")
    if not recipient or parseaddr(recipient)[1] != recipient:
        raise MailDeliveryError("Invalid recipient address")
    if not subject or "\r" in subject or "\n" in subject:
        raise MailDeliveryError("Invalid subject")

    message = EmailMessage()
    message["From"] = settings.smtp_from
    message["To"] = recipient
    message["Subject"] = f"[{ticket_id}] {subject}"
    message["X-Minima-Ticket"] = ticket_id
    if settings.smtp_reply_to:
        message["Reply-To"] = settings.smtp_reply_to
    message.set_content(
        f"{body.strip()}\n\n—\nКоманда поддержки Minima\nНомер обращения: {ticket_id}"
    )

    try:
        if settings.smtp_ssl:
            with smtplib.SMTP_SSL(
                settings.smtp_host,
                settings.smtp_port,
                timeout=settings.smtp_timeout,
                context=ssl.create_default_context(),
            ) as server:
                _authenticate_and_send(server, settings, message)
        else:
            with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=settings.smtp_timeout) as server:
                server.ehlo()
                if settings.smtp_starttls:
                    server.starttls(context=ssl.create_default_context())
                    server.ehlo()
                _authenticate_and_send(server, settings, message)
    except (smtplib.SMTPException, OSError, TimeoutError) as exc:
        # Do not return server responses, usernames, or other SMTP details to the browser.
        raise MailDeliveryError(type(exc).__name__) from exc


def _authenticate_and_send(server: smtplib.SMTP, settings: Settings, message: EmailMessage) -> None:
    if settings.smtp_username:
        server.login(settings.smtp_username, settings.smtp_password)
    server.send_message(message)
