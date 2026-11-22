"""Outgoing mail via the configured SMTP server.

Supports implicit TLS (SMTPS, port 465), STARTTLS (port 587) and plain connections,
TLS 1.2+ with certificate verification, SMTP AUTH and optional DKIM signing.
"""

from __future__ import annotations

import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
from pathlib import Path
from typing import Any

import aiosmtplib
from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlalchemy.ext.asyncio import AsyncSession

from mmn.i18n import t
from mmn.security import decrypt_secret
from mmn.services.settings_store import get_setting

_env = Environment(
    loader=FileSystemLoader(Path(__file__).resolve().parent.parent / "mail_templates"),
    autoescape=select_autoescape(["html"]),
)


class MailNotConfigured(Exception):
    pass


@dataclass
class MailContent:
    subject: str
    title: str
    paragraphs: list[str]
    muted: list[str]
    button_url: str | None = None
    button_label: str | None = None


def render(content: MailContent, *, app: str, lang: str) -> tuple[str, str]:
    footer = t(lang, "footer", app=app)
    html = _env.get_template("layout.html").render(
        lang=lang, app=app, title=content.title, paragraphs=content.paragraphs, muted=content.muted,
        button_url=content.button_url, button_label=content.button_label, footer=footer,
    )
    text_parts = [content.title, "", *content.paragraphs]
    if content.button_url:
        text_parts += ["", f"{content.button_label}: {content.button_url}"]
    text_parts += ["", *content.muted, "", "--", footer]
    return html, "\n".join(text_parts)


def _tls_context(cfg: dict[str, Any]) -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.minimum_version = ssl.TLSVersion.TLSv1_3 if cfg.get("min_tls") == "1.3" else ssl.TLSVersion.TLSv1_2
    if not cfg.get("verify_tls", True):
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


def build_message(cfg: dict[str, Any], to: list[str], subject: str, html: str, text: str) -> EmailMessage:
    msg = EmailMessage()
    from_addr = cfg["from_address"]
    msg["From"] = formataddr((cfg.get("from_name") or "", from_addr))
    msg["To"] = ", ".join(to)
    msg["Subject"] = subject
    if cfg.get("reply_to"):
        msg["Reply-To"] = cfg["reply_to"]
    domain = from_addr.split("@")[-1] if "@" in from_addr else None
    msg["Message-ID"] = make_msgid(domain=domain)
    msg["Auto-Submitted"] = "auto-generated"
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")
    return msg


def dkim_sign(cfg: dict[str, Any], raw: bytes) -> bytes:
    import dkim

    key = decrypt_secret(cfg.get("dkim_key_enc"))
    if not key:
        return raw
    sig = dkim.sign(
        raw,
        selector=cfg["dkim_selector"].encode(),
        domain=cfg["dkim_domain"].encode(),
        privkey=key.encode(),
        include_headers=[b"from", b"to", b"subject", b"date", b"message-id", b"reply-to"],
    )
    return sig + raw


async def send_with_config(cfg: dict[str, Any], to: list[str], subject: str, html: str, text: str) -> None:
    if not cfg.get("host") or not cfg.get("from_address"):
        raise MailNotConfigured("SMTP host and from address are required")
    msg = build_message(cfg, to, subject, html, text)
    if "Date" not in msg:
        from email.utils import formatdate

        msg["Date"] = formatdate(localtime=False)
    security = cfg.get("security", "starttls")
    kwargs: dict[str, Any] = {
        "hostname": cfg["host"],
        "port": int(cfg.get("port") or (465 if security == "tls" else 587)),
        "timeout": int(cfg.get("timeout") or 20),
        "use_tls": security == "tls",
        "start_tls": True if security == "starttls" else False,
    }
    if security in ("tls", "starttls"):
        kwargs["tls_context"] = _tls_context(cfg)
    username = cfg.get("username") or None
    if username:
        kwargs["username"] = username
        kwargs["password"] = decrypt_secret(cfg.get("password_enc")) or ""
    raw = msg.as_bytes()
    if cfg.get("dkim_enabled") and cfg.get("dkim_domain") and cfg.get("dkim_selector"):
        raw = dkim_sign(cfg, raw)
    await aiosmtplib.send(raw, sender=cfg["from_address"], recipients=to, **kwargs)


async def send_mail(db: AsyncSession, to: list[str] | str, content: MailContent, lang: str = "en") -> None:
    cfg = await get_setting(db, "mail")
    if not cfg.get("enabled"):
        raise MailNotConfigured("Mail is disabled")
    general = await get_setting(db, "general")
    recipients = [to] if isinstance(to, str) else to
    html, text = render(content, app=general["app_name"], lang=lang)
    await send_with_config(cfg, recipients, content.subject, html, text)


async def try_send_mail(db: AsyncSession, to: list[str] | str, content: MailContent, lang: str = "en") -> bool:
    import logging

    try:
        await send_mail(db, to, content, lang)
        return True
    except MailNotConfigured:
        return False
    except Exception:  # noqa: BLE001 - notifications must never break the caller
        logging.getLogger("mmn.mail").exception("Sending notification failed")
        return False
