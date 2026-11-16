"""Email a run's results: a short summary in the body and the spreadsheet attached.

Sending goes through any SMTP server (Gmail, Outlook, Fastmail, SendGrid, ...), using the
standard library only. The server details come from the environment, like the API keys.
"""

from __future__ import annotations

import os
import smtplib
import ssl
from dataclasses import dataclass
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Callable

from .config import ConfigError, Settings
from .costs import format_costs

XLSX_TYPE = ("application", "vnd.openxmlformats-officedocument.spreadsheetml.sheet")


class EmailError(RuntimeError):
    """The email could not be sent. The run's files are still on disk."""


@dataclass
class SmtpConfig:
    host: str = ""
    port: int = 587
    username: str = ""
    password: str = ""
    sender: str = ""

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "SmtpConfig":
        env = os.environ if env is None else env
        port = env.get("SMTP_PORT", "").strip() or "587"
        if not port.isdigit():
            raise ConfigError("SMTP_PORT must be a number, for example 587.")
        username = env.get("SMTP_USERNAME", "").strip()
        return cls(
            host=env.get("SMTP_HOST", "").strip(),
            port=int(port),
            username=username,
            password=env.get("SMTP_PASSWORD", "").strip(),
            sender=env.get("EMAIL_FROM", "").strip() or username,
        )

    def check(self) -> None:
        missing = [
            name
            for name, value in (("SMTP_HOST", self.host), ("SMTP_USERNAME", self.username),
                                ("SMTP_PASSWORD", self.password))
            if not value
        ]
        if missing:
            raise ConfigError(f"To send email, set {', '.join(missing)} in .env (see .env.example).")


def recipients(settings: Settings, override: str | None = None, env: dict[str, str] | None = None) -> list[str]:
    """Who gets the email: --email-to, else EMAIL_TO in the environment, else `email.to` in config."""
    env = os.environ if env is None else env
    raw = override or env.get("EMAIL_TO", "")
    found = [part.strip() for part in raw.replace(";", ",").split(",") if part.strip()] or list(settings.email.to)
    if not found:
        raise ConfigError("No email recipients. Set `email.to` in config.yaml, EMAIL_TO in .env, or pass --email-to.")
    bad = [address for address in found if "@" not in address or " " in address]
    if bad:
        raise ConfigError(f"Not a valid email address: {', '.join(bad)}")
    return list(dict.fromkeys(found))


def compose(result: Any, settings: Settings, *, sender: str, to: list[str], when: datetime, only_new: bool) -> EmailMessage:
    """Build the message. `result` is the RunResult from the pipeline."""
    leads = list(result.lead_items)
    new = "new " if only_new else ""
    day = f"{when:%b} {when.day}"
    window = "the last day" if settings.days == 1 else f"the last {settings.days} days"

    lines: list[str] = []
    if leads:
        breakdown = ", ".join(f"{count} {level}" for level, count in result.leads_by_confidence.items())
        subject = f"Investor leads, {day}: {len(leads)} {new}({breakdown})"
        lines.append(f"{len(leads)} {new}investor leads from posts in {window}: {breakdown}.")
    else:
        subject = f"Investor leads, {day}: no {new}leads"
        lines.append(f"No {new}investor leads today. The run completed normally and found nothing that met the bar.")
    lines += [
        "",
        f"Industries: {', '.join(settings.industries)}",
        f"Stages:     {', '.join(settings.stages)}",
        f"Posts read: {result.posts_considered:,}",
    ]

    if leads:
        shown = leads[: settings.email.max_leads_in_body]
        lines += ["", "Strongest leads:" if len(leads) > len(shown) else "Leads:"]
        for number, lead in enumerate(shown, start=1):
            who = f"{lead.name} ({lead.firm})" if lead.firm else lead.name
            lines.append(f"{number}. [{lead.confidence}] {who}: {lead.signal}")
            if lead.why:
                lines.append(f"   {_shorten(lead.why, 220)}")
            lines.append(f"   {lead.post.url}")
        if len(leads) > len(shown):
            lines.append(f"...and {len(leads) - len(shown)} more in the attached spreadsheet.")
        else:
            lines += ["", "The attached spreadsheet has the full details."]

    if result.warnings:
        lines += ["", "Warnings from the run:"] + [f"- {warning}" for warning in result.warnings]
    costs = format_costs(result.costs)
    if costs:
        lines += [""] + costs

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = sender
    message["To"] = ", ".join(to)
    message.set_content("\n".join(lines) + "\n")
    if leads and result.xlsx_path:
        path = Path(result.xlsx_path)
        message.add_attachment(path.read_bytes(), maintype=XLSX_TYPE[0], subtype=XLSX_TYPE[1], filename=path.name)
    return message


def _shorten(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def send(message: EmailMessage, smtp: SmtpConfig, connect: Callable[..., Any] | None = None) -> None:
    """Send through the configured SMTP server. Port 465 uses TLS from the start; others upgrade with STARTTLS."""
    context = ssl.create_default_context()
    try:
        if smtp.port == 465:
            server = (connect or smtplib.SMTP_SSL)(smtp.host, smtp.port, timeout=60, context=context)
        else:
            server = (connect or smtplib.SMTP)(smtp.host, smtp.port, timeout=60)
        with server:
            if smtp.port != 465:
                server.starttls(context=context)
            server.login(smtp.username, smtp.password)
            server.send_message(message)
    except smtplib.SMTPAuthenticationError as exc:
        raise EmailError(
            "The mail server rejected the username or password. For Gmail, SMTP_PASSWORD must be an "
            "app password, not your normal password."
        ) from exc
    except (smtplib.SMTPException, OSError) as exc:
        raise EmailError(f"Could not send the email through {smtp.host}:{smtp.port}: {exc}") from exc
