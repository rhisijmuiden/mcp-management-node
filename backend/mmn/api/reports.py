"""Monthly customer reports (phase 10): settings, generate on demand, view, download and mail."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy.ext.asyncio import AsyncSession

from mmn.api.deps import client_ip, current_user, require_admin
from mmn.db import get_db
from mmn.models import Environment, User
from mmn.services import nodes, reports
from mmn.services.audit import audit
from mmn.services.mailer import MailNotConfigured

router = APIRouter(prefix="/api/customers", tags=["reports"])


async def _cust(db: AsyncSession, cid: str) -> Environment:
    try:
        return await nodes.get_environment(db, cid)
    except nodes.NodeError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(e)) from e


async def _report(db: AsyncSession, e: Environment, rid: str):
    try:
        r = await reports.get_report(db, rid)
    except nodes.NodeError as ex:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(ex)) from ex
    if r.environment_id != e.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="report not found")
    return r


@router.get("/{cid}/reports")
async def list_reports(cid: str, db: AsyncSession = Depends(get_db), _: User = Depends(current_user)):
    e = await _cust(db, cid)
    tz = await reports.timezone(db)
    now = datetime.now(UTC)
    return {"settings": reports.settings_of(e),
            "reports": [reports.report_out(r) for r in await reports.list_reports(db, e.id)],
            "previous_period": reports.previous_period(now, tz), "current_period": reports.current_period(now, tz)}


class ReportSettingsIn(BaseModel):
    enabled: bool = False
    day: int = Field(default=1, ge=1, le=28)
    recipients: list[EmailStr] = Field(default_factory=list, max_length=20)
    lang: str = Field(default="en", pattern=r"^(en|nl)$")
    include_ai: bool = True


@router.put("/{cid}/reports/settings")
async def save_settings(cid: str, body: ReportSettingsIn, request: Request, db: AsyncSession = Depends(get_db),
                        admin: User = Depends(require_admin)):
    e = await _cust(db, cid)
    e.report = {**body.model_dump(), "recipients": [str(x) for x in body.recipients]}
    await audit(db, "report.settings", user=admin, target=e.name, ip=client_ip(request), enabled=body.enabled,
                day=body.day, recipients=len(body.recipients))
    await db.commit()
    return reports.settings_of(e)


class GenerateIn(BaseModel):
    period: str = Field(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")
    lang: str | None = Field(default=None, pattern=r"^(en|nl)$")
    send: bool = False


@router.post("/{cid}/reports", status_code=201)
async def generate(cid: str, body: GenerateIn, request: Request, db: AsyncSession = Depends(get_db),
                   admin: User = Depends(require_admin)):
    e = await _cust(db, cid)
    try:
        r = await reports.generate(db, e, body.period, lang=body.lang, trigger="manual", created_by=admin.username)
    except nodes.NodeError as ex:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(ex)) from ex
    await audit(db, "report.generate", user=admin, target=e.name, ip=client_ip(request), period=body.period)
    await db.commit()
    if body.send:
        await _send(db, e, r, None, admin, request)
    return reports.report_out(r)


@router.get("/{cid}/reports/{rid}")
async def get_report(cid: str, rid: str, db: AsyncSession = Depends(get_db), _: User = Depends(current_user)):
    e = await _cust(db, cid)
    return reports.report_out(await _report(db, e, rid), with_data=True)


@router.get("/{cid}/reports/{rid}/html", response_class=HTMLResponse)
async def report_html(cid: str, rid: str, download: bool = False, db: AsyncSession = Depends(get_db),
                      _: User = Depends(current_user)):
    e = await _cust(db, cid)
    r = await _report(db, e, rid)
    headers = {"Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; img-src data:",
               "X-Content-Type-Options": "nosniff"}
    if download:
        headers["Content-Disposition"] = f'attachment; filename="{reports.filename(r, e.name, "html")}"'
    return HTMLResponse(r.html, headers=headers)


@router.get("/{cid}/reports/{rid}/pdf")
async def report_pdf(cid: str, rid: str, download: bool = True, db: AsyncSession = Depends(get_db),
                     _: User = Depends(current_user)):
    e = await _cust(db, cid)
    r = await _report(db, e, rid)
    if not r.pdf:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="no PDF for this report")
    disp = "attachment" if download else "inline"
    return Response(bytes(r.pdf), media_type="application/pdf",
                    headers={"Content-Disposition": f'{disp}; filename="{reports.filename(r, e.name, "pdf")}"'})


class SendIn(BaseModel):
    to: list[EmailStr] | None = Field(default=None, max_length=20)


async def _send(db: AsyncSession, e: Environment, r, to: list[str] | None, admin: User, request: Request) -> list[str]:
    try:
        sent = await reports.send(db, r, e, to)
    except nodes.NodeError as ex:
        await db.commit()
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(ex)) from ex
    except MailNotConfigured as ex:
        await db.commit()
        raise HTTPException(status.HTTP_409_CONFLICT, detail="mail_not_configured") from ex
    except Exception as ex:  # noqa: BLE001
        await audit(db, "report.send", user=admin, target=e.name, ip=client_ip(request), success=False,
                    period=r.period, error=str(ex)[:300])
        await db.commit()
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail=f"mail failed: {ex}") from ex
    await audit(db, "report.send", user=admin, target=e.name, ip=client_ip(request), period=r.period, to=sent)
    await db.commit()
    return sent


@router.post("/{cid}/reports/{rid}/send")
async def send_report(cid: str, rid: str, body: SendIn, request: Request, db: AsyncSession = Depends(get_db),
                      admin: User = Depends(require_admin)):
    e = await _cust(db, cid)
    r = await _report(db, e, rid)
    sent = await _send(db, e, r, [str(x) for x in body.to] if body.to else None, admin, request)
    return {"sent_to": sent, "report": reports.report_out(r)}


@router.delete("/{cid}/reports/{rid}", status_code=204)
async def delete_report(cid: str, rid: str, request: Request, db: AsyncSession = Depends(get_db),
                        admin: User = Depends(require_admin)):
    e = await _cust(db, cid)
    r = await _report(db, e, rid)
    await db.delete(r)
    await audit(db, "report.delete", user=admin, target=e.name, ip=client_ip(request), period=r.period)
    await db.commit()
    return Response(status_code=204)
