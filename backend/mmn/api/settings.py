from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, EmailStr, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mmn.api.deps import client_ip, current_user, require_admin
from mmn.db import get_db
from mmn.models import AuditLog, User
from mmn.services.audit import audit
from mmn.services.mail_settings import MailSettingsIn, get_mail_settings, send_test_mail, update_mail_settings
from mmn.services.settings_store import get_setting, set_setting

router = APIRouter(prefix="/api/settings", tags=["settings"])


# ---------- general ----------
class GeneralIn(BaseModel):
    app_name: str | None = Field(default=None, min_length=1, max_length=64)
    base_url: str | None = Field(default=None, max_length=255)
    default_language: Literal["en", "nl"] | None = None

    @field_validator("base_url")
    @classmethod
    def _url(cls, v: str | None) -> str | None:
        if v is None or v == "":
            return v
        if not v.startswith(("http://", "https://")):
            raise ValueError("must start with http:// or https://")
        return v.rstrip("/")


@router.get("/general")
async def get_general(db: AsyncSession = Depends(get_db), _: User = Depends(current_user)):
    return await get_setting(db, "general")


@router.put("/general")
async def put_general(body: GeneralIn, request: Request, db: AsyncSession = Depends(get_db),
                      admin: User = Depends(require_admin)):
    value = await set_setting(db, "general", body.model_dump(exclude_none=True))
    await audit(db, "settings.general", user=admin, ip=client_ip(request))
    await db.commit()
    return value


# ---------- notifications ----------
class NotificationsIn(BaseModel):
    backup_failure_to: EmailStr | Literal[""] | None = None
    backup_success_to: EmailStr | Literal[""] | None = None
    update_to: EmailStr | Literal[""] | None = None


@router.get("/notifications")
async def get_notifications(db: AsyncSession = Depends(get_db), _: User = Depends(require_admin)):
    return await get_setting(db, "notifications")


@router.put("/notifications")
async def put_notifications(body: NotificationsIn, request: Request, db: AsyncSession = Depends(get_db),
                            admin: User = Depends(require_admin)):
    value = await set_setting(db, "notifications",
                              {k: str(v) for k, v in body.model_dump(exclude_none=True).items()})
    await audit(db, "settings.notifications", user=admin, ip=client_ip(request))
    await db.commit()
    return value


# ---------- mail ----------
@router.get("/mail")
async def get_mail(db: AsyncSession = Depends(get_db), _: User = Depends(require_admin)):
    return await get_mail_settings(db)


@router.put("/mail")
async def put_mail(body: MailSettingsIn, request: Request, db: AsyncSession = Depends(get_db),
                   admin: User = Depends(require_admin)):
    try:
        value = await update_mail_settings(db, body)
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(e)) from e
    await audit(db, "settings.mail", user=admin, ip=client_ip(request))
    await db.commit()
    return value


class TestMailIn(BaseModel):
    to: EmailStr
    settings: MailSettingsIn | None = None


@router.post("/mail/test")
async def test_mail(body: TestMailIn, request: Request, db: AsyncSession = Depends(get_db),
                    admin: User = Depends(require_admin)):
    try:
        await send_test_mail(db, str(body.to), admin.language, body.settings)
    except Exception as e:  # noqa: BLE001 - show the SMTP error to the admin
        await audit(db, "mail.test", user=admin, target=str(body.to), ip=client_ip(request), success=False,
                    error=str(e)[:500])
        await db.commit()
        return {"ok": False, "error": f"{type(e).__name__}: {e}"[:1000]}
    await audit(db, "mail.test", user=admin, target=str(body.to), ip=client_ip(request))
    await db.commit()
    return {"ok": True}


# ---------- audit log ----------
@router.get("/audit")
async def audit_log(limit: int = Query(100, ge=1, le=500), before: int | None = None,
                    source: str | None = None, q_text: str | None = Query(None, alias="q"),
                    success: bool | None = None,
                    db: AsyncSession = Depends(get_db), _: User = Depends(require_admin)):
    q = select(AuditLog).order_by(AuditLog.id.desc()).limit(limit)
    if before:
        q = q.where(AuditLog.id < before)
    if source:
        q = q.where(AuditLog.source == source)
    if success is not None:
        q = q.where(AuditLog.success.is_(success))
    if q_text:
        like = f"%{q_text[:100]}%"
        q = q.where(AuditLog.action.ilike(like) | AuditLog.target.ilike(like) | AuditLog.username.ilike(like))
    rows = (await db.execute(q)).scalars().all()
    return [{"id": r.id, "at": r.at, "username": r.username, "source": r.source, "action": r.action,
             "target": r.target, "ip": r.ip, "success": r.success, "detail": r.detail} for r in rows]
