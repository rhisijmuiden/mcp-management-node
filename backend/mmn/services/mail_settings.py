"""Reading and changing the app's own SMTP settings (shared by the REST API and MCP)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, EmailStr, Field
from sqlalchemy.ext.asyncio import AsyncSession

from mmn.i18n import t
from mmn.security import encrypt_secret
from mmn.services.mailer import MailContent, render, send_with_config
from mmn.services.settings_store import get_setting, set_setting


class MailSettingsIn(BaseModel):
    enabled: bool | None = None
    host: str | None = Field(default=None, max_length=255)
    port: int | None = Field(default=None, ge=1, le=65535)
    security: Literal["none", "starttls", "tls"] | None = None
    username: str | None = Field(default=None, max_length=255)
    # None = keep, "" = clear, otherwise new value
    password: str | None = Field(default=None, max_length=1024)
    from_name: str | None = Field(default=None, max_length=128)
    from_address: EmailStr | None = None
    reply_to: EmailStr | Literal[""] | None = None
    verify_tls: bool | None = None
    min_tls: Literal["1.2", "1.3"] | None = None
    timeout: int | None = Field(default=None, ge=3, le=120)
    dkim_enabled: bool | None = None
    dkim_domain: str | None = Field(default=None, max_length=255)
    dkim_selector: str | None = Field(default=None, max_length=63)
    dkim_private_key: str | None = Field(default=None, max_length=10000)


def public_view(cfg: dict[str, Any]) -> dict[str, Any]:
    out = {k: v for k, v in cfg.items() if not k.endswith("_enc")}
    out["password_set"] = bool(cfg.get("password_enc"))
    out["dkim_key_set"] = bool(cfg.get("dkim_key_enc"))
    return out


async def get_mail_settings(db: AsyncSession) -> dict[str, Any]:
    return public_view(await get_setting(db, "mail"))


def _apply(current: dict[str, Any], body: MailSettingsIn) -> dict[str, Any]:
    data = body.model_dump(exclude_unset=True)
    new = dict(current)
    if "password" in data:
        pw = data.pop("password")
        if pw is not None:
            new["password_enc"] = encrypt_secret(pw) if pw else None
    if "dkim_private_key" in data:
        key = data.pop("dkim_private_key")
        if key is not None:
            new["dkim_key_enc"] = encrypt_secret(key.strip()) if key.strip() else None
    for k, v in data.items():
        if v is not None:
            new[k] = str(v) if k in ("from_address", "reply_to") else v
    return new


async def update_mail_settings(db: AsyncSession, body: MailSettingsIn) -> dict[str, Any]:
    current = await get_setting(db, "mail")
    new = _apply(current, body)
    if new.get("dkim_enabled"):
        if not (new.get("dkim_domain") and new.get("dkim_selector") and new.get("dkim_key_enc")):
            raise ValueError("dkim_incomplete")
        from mmn.security import decrypt_secret

        key = decrypt_secret(new["dkim_key_enc"]) or ""
        from cryptography.hazmat.primitives.serialization import load_pem_private_key

        try:
            load_pem_private_key(key.encode(), password=None)
        except Exception as e:  # noqa: BLE001
            raise ValueError("dkim_key_invalid") from e
    saved = await set_setting(db, "mail", new)
    return public_view(saved)


async def send_test_mail(db: AsyncSession, to: str, lang: str, override: MailSettingsIn | None = None) -> None:
    """Send a test mail. With ``override`` the unsaved values from the form are tested."""
    cfg = await get_setting(db, "mail")
    if override is not None:
        cfg = _apply(cfg, override)
    general = await get_setting(db, "general")
    app = general["app_name"]
    content = MailContent(
        subject=t(lang, "test.subject", app=app), title=t(lang, "test.title"),
        paragraphs=[t(lang, "test.body", app=app)],
        muted=[t(lang, "test.details", host=cfg.get("host"), port=cfg.get("port"),
                 security=str(cfg.get("security")).upper())],
    )
    html, text = render(content, app=app, lang=lang)
    await send_with_config(cfg, [to], content.subject, html, text)
