"""
lib/mailer.py — отправка писем и алертов через SMTP_SSL (Яндекс).

Правило: ошибки SMTP логируются, но НЕ пробрасываются — мы не хотим
падать из-за упавшего почтового сервера.
"""
from __future__ import annotations

import mimetypes
import smtplib
from email.message import EmailMessage

from lib import config
from lib.logging_setup import get_logger

log = get_logger("mailer")


def send_email(to: str, subject: str, body: str, attachments=None) -> bool:
    """
    attachments: list[tuple[str filename, bytes data]].
    Возвращает True при успехе, False при ошибке (исключений не бросает).
    """
    try:
        config.require("SMTP_USER", "SMTP_PASSWORD")
    except config.ConfigError as e:
        log.error("mail not configured: %s", e)
        return False

    msg = EmailMessage()
    msg["From"] = config.SMTP_USER
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)

    for fname, data in attachments or []:
        ctype, _ = mimetypes.guess_type(fname)
        maintype, subtype = (
            ctype.split("/", 1) if ctype else ("application", "octet-stream")
        )
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=fname)

    try:
        with smtplib.SMTP_SSL(config.SMTP_HOST, config.SMTP_PORT, timeout=30) as srv:
            srv.login(config.SMTP_USER, config.SMTP_PASSWORD)
            srv.send_message(msg)
        log.info("sent '%s' -> %s", subject, to)
        return True
    except Exception as e:  # noqa: BLE001 — намеренно глушим любую ошибку SMTP
        log.error("SMTP send failed (%s): %s", subject, e)
        return False


def alert(subject: str, body: str) -> bool:
    """Быстрый алерт на ALERT_TO (или SMTP_USER, если ALERT_TO не задан)."""
    to = config.ALERT_TO or config.SMTP_USER or ""
    return send_email(to, f"[wb-lutner] {subject}", body)
