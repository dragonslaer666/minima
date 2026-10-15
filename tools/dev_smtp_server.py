"""Локальный SMTP-приёмник для разработки Minima.

Принимает письма по SMTP на 127.0.0.1 и складывает их в data/dev-mail/,
не отправляя ничего наружу. Нужен, чтобы проверить отправку кодов
подтверждения и ответов на обращения без доступа к реальному почтовому
провайдеру.

Запуск:

    .venv/bin/python tools/dev_smtp_server.py --host 127.0.0.1 --port 2525

В .env при этом указываются:

    SMTP_HOST=127.0.0.1
    SMTP_PORT=2525
    SMTP_STARTTLS=false
    SMTP_SSL=false
    SMTP_USERNAME=
    SMTP_PASSWORD=
    SMTP_FROM=Minima Support <support@minima.local>

Это development-инструмент: он не ретранслирует почту и не хранит пароли.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime
from email import message_from_bytes
from email.header import decode_header, make_header
from email.utils import parseaddr
from pathlib import Path

from aiosmtpd.controller import Controller


def _decode(value: str) -> str:
    if not value:
        return ""
    return str(make_header(decode_header(value)))


class DevMailHandler:
    """Сохраняет каждое письмо отдельным .eml-файлом и печатает сводку."""

    def __init__(self, mail_dir: Path) -> None:
        self.mail_dir = mail_dir
        self.mail_dir.mkdir(parents=True, exist_ok=True)
        self.counter = 0

    async def handle_DATA(self, server, session, envelope) -> str:  # noqa: N802 (aiosmtpd API)
        self.counter += 1
        message = message_from_bytes(envelope.content)
        subject = _decode(message.get("Subject", ""))
        recipients = envelope.rcpt_tos or []
        if isinstance(recipients, str):
            recipients = [recipients]
        recipient = ", ".join(sorted({parseaddr(str(item))[1] for item in recipients}))
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        path = self.mail_dir / f"{stamp}-{self.counter:04d}.eml"
        path.write_bytes(envelope.content)
        body = message.get_payload(decode=True)
        text = body.decode(message.get_content_charset() or "utf-8", errors="replace") if body else ""
        print(f"\n=== Письмо {self.counter}: {path.name}", flush=True)
        print(f"От:    {_decode(message.get('From', ''))}", flush=True)
        print(f"Кому:  {recipient}", flush=True)
        print(f"Тема:  {subject}", flush=True)
        print("--- текст ---", flush=True)
        print(text.strip(), flush=True)
        print("--- конец ---", flush=True)
        return "250 Message accepted for delivery"


def main() -> None:
    parser = argparse.ArgumentParser(description="Локальный SMTP-приёмник Minima для разработки")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=2525)
    parser.add_argument("--mail-dir", default="data/dev-mail", type=Path)
    args = parser.parse_args()

    handler = DevMailHandler(args.mail_dir)
    controller = Controller(handler, hostname=args.host, port=args.port)
    controller.start()
    print(f"SMTP-приёмник слушает {args.host}:{args.port}, письма сохраняются в {args.mail_dir}", flush=True)
    try:
        asyncio.get_event_loop().run_forever()
    except KeyboardInterrupt:
        pass
    finally:
        controller.stop()


if __name__ == "__main__":
    main()
