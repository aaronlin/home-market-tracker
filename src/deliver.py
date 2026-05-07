from __future__ import annotations

import os
import socket
import smtplib
import time
from email.message import EmailMessage
from typing import Optional


SMTP_ENV = [
    "HOME_MARKET_SMTP_HOST",
    "HOME_MARKET_SMTP_PORT",
    "HOME_MARKET_SMTP_USER",
    "HOME_MARKET_SMTP_PASSWORD",
    "HOME_MARKET_EMAIL_TO",
    "HOME_MARKET_EMAIL_FROM",
]
DEFAULT_SMTP_TIMEOUT_SECONDS = 30
DEFAULT_SMTP_RETRIES = 2


def missing_smtp_env() -> list[str]:
    return [name for name in SMTP_ENV if not os.environ.get(name)]


def send_email(subject: str, html_body: str, text_body: str, dry_run: bool = False) -> Optional[str]:
    if dry_run:
        return "SMTP skipped because this was a dry run."
    missing = missing_smtp_env()
    if missing:
        return f"SMTP skipped; missing env vars: {', '.join(missing)}"

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = os.environ["HOME_MARKET_EMAIL_FROM"]
    message["To"] = os.environ["HOME_MARKET_EMAIL_TO"]
    message.set_content(text_body)
    message.add_alternative(html_body, subtype="html")

    host = os.environ["HOME_MARKET_SMTP_HOST"]
    port = int(os.environ["HOME_MARKET_SMTP_PORT"])
    timeout = _smtp_timeout_seconds()
    retries = _smtp_retry_count()
    last_error: Optional[str] = None
    for attempt in range(1, retries + 1):
        try:
            with smtplib.SMTP(host, port, timeout=timeout) as smtp:
                smtp.starttls()
                smtp.login(os.environ["HOME_MARKET_SMTP_USER"], os.environ["HOME_MARKET_SMTP_PASSWORD"])
                smtp.send_message(message)
            return None
        except smtplib.SMTPAuthenticationError:
            return "SMTP login failed. Check HOME_MARKET_SMTP_USER and HOME_MARKET_SMTP_PASSWORD."
        except (TimeoutError, socket.timeout):
            last_error = f"SMTP connection timed out after {timeout} seconds while contacting {host}:{port}."
        except smtplib.SMTPServerDisconnected as exc:
            last_error = f"SMTP server disconnected unexpectedly while contacting {host}:{port}: {exc}"
        except OSError as exc:
            last_error = f"SMTP connection failed while contacting {host}:{port}: {exc}"
        if attempt < retries:
            time.sleep(min(attempt, 2))
    return last_error or "SMTP send failed for an unknown reason."


def _smtp_timeout_seconds() -> int:
    raw_value = os.environ.get("HOME_MARKET_SMTP_TIMEOUT_SECONDS", str(DEFAULT_SMTP_TIMEOUT_SECONDS))
    try:
        parsed = int(raw_value)
    except ValueError:
        return DEFAULT_SMTP_TIMEOUT_SECONDS
    return parsed if parsed > 0 else DEFAULT_SMTP_TIMEOUT_SECONDS


def _smtp_retry_count() -> int:
    raw_value = os.environ.get("HOME_MARKET_SMTP_RETRIES", str(DEFAULT_SMTP_RETRIES))
    try:
        parsed = int(raw_value)
    except ValueError:
        return DEFAULT_SMTP_RETRIES
    return parsed if parsed > 0 else DEFAULT_SMTP_RETRIES
