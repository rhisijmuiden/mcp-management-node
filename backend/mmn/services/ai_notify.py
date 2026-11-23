"""Notifications for approval requests. Channels are pluggable: each one registers itself in
CHANNELS with a name, the setting that switches it on, and a send function. Built in: "app" (live
event in the web interface) and "mail" (the configured SMTP server)."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mmn.i18n import t
from mmn.models import AiApproval, Role, User
from mmn.services import events
from mmn.services.settings_store import get_setting

log = logging.getLogger("mmn.gateway")


@dataclass
class Notice:
    approval: AiApproval | None
    url: str | None
    test: bool = False


@dataclass
class Channel:
    name: str
    setting: str  # key in the ai_gateway settings that switches it on
    send: Callable[[AsyncSession, dict[str, Any], Notice], Awaitable[None]]


CHANNELS: dict[str, Channel] = {}


def register(ch: Channel) -> None:
    CHANNELS[ch.name] = ch


async def _app(db: AsyncSession, cfg: dict[str, Any], n: Notice) -> None:
    a = n.approval
    events.publish("approval.created", {"id": a.id if a else None, "tool": a.tool if a else None,
                                        "who": (a.client or {}).get("name") or (a.token_name if a else None),
                                        "test": n.test})


async def _recipients(db: AsyncSession, cfg: dict[str, Any]) -> list[tuple[str, str]]:
    """(address, language). The configured list, or every active admin with an address."""
    configured = [x.strip() for x in (cfg.get("notify_mail_to") or "").replace(";", ",").split(",") if x.strip()]
    admins = (await db.execute(select(User).where(User.role == Role.admin, User.is_active.is_(True)))).scalars().all()
    lang_of = {u.email.lower(): u.language for u in admins if u.email}
    if configured:
        return [(x, lang_of.get(x.lower(), "en")) for x in configured]
    return [(u.email, u.language or "en") for u in admins if u.email]


async def _mail(db: AsyncSession, cfg: dict[str, Any], n: Notice) -> None:
    from mmn.services.mailer import MailContent, MailNotConfigured, send_mail

    if not (await get_setting(db, "mail")).get("enabled"):
        if n.test:
            raise MailNotConfigured("Mail is disabled")
        return
    app = (await get_setting(db, "general"))["app_name"]
    by_lang: dict[str, list[str]] = {}
    for addr, lang in await _recipients(db, cfg):
        by_lang.setdefault(lang if lang in ("en", "nl") else "en", []).append(addr)
    from mmn.services.gateway import reason_text

    for lang, to in by_lang.items():
        a = n.approval
        if n.test or a is None:
            content = MailContent(subject=t(lang, "approval.test_subject", app=app), title=t(lang, "approval.title"),
                                  paragraphs=[t(lang, "approval.test_body")], muted=[])
        else:
            who = (a.client or {}).get("name") or a.username or "AI"
            paras = [t(lang, "approval.body", who=who, token=a.token_name or "-", tool=a.tool, risk=a.risk,
                       target=a.target or ", ".join(a.customer_names or []) or "-")]
            if a.ai_reason:
                paras.append(t(lang, "approval.reason", reason=a.ai_reason))
            if a.reasons:
                paras.append(t(lang, "approval.why", why="; ".join(reason_text(r) for r in a.reasons)))
            content = MailContent(subject=t(lang, "approval.subject", tool=a.tool, who=who), title=t(lang, "approval.title"),
                                  paragraphs=paras,
                                  muted=[t(lang, "approval.expires", expires=a.expires_at.strftime("%Y-%m-%d %H:%M"))],
                                  button_url=n.url, button_label=t(lang, "approval.button") if n.url else None)
        await send_mail(db, to, content, lang)


register(Channel("app", "notify_app", _app))
register(Channel("mail", "notify_mail", _mail))


async def approval_url(db: AsyncSession, approval_id: str | None) -> str | None:
    from mmn.services.links import public_base_url

    base = await public_base_url(db)
    if not base:
        return None
    return f"{base}/ai/approvals" + (f"?id={approval_id}" if approval_id else "")


async def notify(db: AsyncSession, approval: AiApproval) -> dict[str, bool]:
    """Send a new approval request to every enabled channel. Never raises."""
    cfg = await get_setting(db, "ai_gateway")
    notice = Notice(approval=approval, url=await approval_url(db, approval.id))
    sent: dict[str, bool] = {}
    for ch in CHANNELS.values():
        if not cfg.get(ch.setting):
            continue
        try:
            await ch.send(db, cfg, notice)
            sent[ch.name] = True
        except Exception:  # noqa: BLE001 - a broken channel must not block the AI call
            log.exception("Approval notification via %s failed", ch.name)
            sent[ch.name] = False
    return sent


async def send_test(db: AsyncSession, channel: str) -> None:
    ch = CHANNELS.get(channel)
    if ch is None:
        raise ValueError("unknown channel")
    cfg = await get_setting(db, "ai_gateway")
    await ch.send(db, cfg, Notice(approval=None, url=await approval_url(db, None), test=True))
