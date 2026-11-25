"""Certificates, firewall changes and VPN tunnels per customer (phase 20)."""

from __future__ import annotations

import re
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from mmn.api.deps import client_ip, current_user, require_admin
from mmn.db import get_db
from mmn.models import (
    CertEndpoint,
    Certificate,
    Environment,
    FirewallSource,
    FirewallVersion,
    User,
    VpnTunnel,
)
from mmn.services import agent_hub, certs, netsec
from mmn.services.audit import audit
from mmn.services.settings_store import get_setting, set_setting

router = APIRouter(prefix="/api/netsec", tags=["netsec"])


def _bad(e: Exception) -> HTTPException:
    return HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(e))


def _missing() -> HTTPException:
    return HTTPException(status.HTTP_404_NOT_FOUND, detail="not_found")


# ------------------------------------------------------------------ overview
@router.get("/overview")
async def overview(customer_id: str | None = None, db: AsyncSession = Depends(get_db),
                   _: User = Depends(current_user)):
    c = await certs.listing(db, customer_id)
    t = await netsec.tunnels(db, customer_id)
    fq = select(func.count(FirewallVersion.id)).where(FirewallVersion.acknowledged_at.is_(None))
    sq = select(func.count(FirewallSource.id))
    if customer_id:
        fq = fq.where(FirewallVersion.customer_id == customer_id)
        sq = sq.where(FirewallSource.customer_id == customer_id)
    down = sum(1 for x in t["tunnels"] if x["status"] in ("down", "missing") and x["watching"])
    unacked = int((await db.execute(fq)).scalar() or 0)
    return {"certificates": c["counts"], "endpoint_errors": len(c["endpoint_errors"]),
            "firewall": {"sources": int((await db.execute(sq)).scalar() or 0), "unacknowledged": unacked},
            "vpn": {**t["counts"], "watched_down": down, "total": len(t["tunnels"])},
            "attention": (c["counts"].get("expired", 0) + c["counts"].get("critical", 0) + unacked + down)}


class RefreshIn(BaseModel):
    customer_id: str
    what: Literal["all", "certificates", "firewall", "vpn"] = "all"


@router.post("/refresh", status_code=202)
async def refresh(body: RefreshIn, request: Request, db: AsyncSession = Depends(get_db),
                  admin: User = Depends(require_admin)):
    e = await db.get(Environment, body.customer_id)
    if e is None:
        raise _missing()
    await audit(db, "netsec.refresh", user=admin, target=e.name, ip=client_ip(request), what=body.what)
    await db.commit()
    agent_hub.spawn(netsec.refresh(e.id, body.what))
    return {"started": True}


# ------------------------------------------------------------------ certificates
@router.get("/certificates")
async def list_certificates(customer_id: str | None = None, status_: str | None = Query(None, alias="status"), source: str | None = None,
                            within_days: int | None = None, db: AsyncSession = Depends(get_db),
                            _: User = Depends(current_user)):
    return await certs.listing(db, customer_id, status_, within_days, source)


class IgnoreIn(BaseModel):
    ignored: bool
    note: str | None = Field(default=None, max_length=300)


@router.post("/certificates/{cid}/ignore")
async def ignore_certificate(cid: str, body: IgnoreIn, request: Request, db: AsyncSession = Depends(get_db),
                             admin: User = Depends(require_admin)):
    c = await db.get(Certificate, cid)
    if c is None:
        raise _missing()
    c.ignored, c.ignored_note = body.ignored, (body.note or "").strip() or None if body.ignored else None
    await audit(db, "netsec.cert_ignore", user=admin, target=c.common_name or c.location, ip=client_ip(request),
                ignored=body.ignored)
    await db.commit()
    return certs.cert_out(c)


@router.delete("/certificates/{cid}", status_code=204)
async def forget_certificate(cid: str, request: Request, db: AsyncSession = Depends(get_db),
                             admin: User = Depends(require_admin)):
    c = await db.get(Certificate, cid)
    if c is None:
        raise _missing()
    await audit(db, "netsec.cert_forget", user=admin, target=c.common_name or c.location, ip=client_ip(request))
    await db.delete(c)
    await db.commit()


@router.get("/endpoints")
async def list_endpoints(customer_id: str | None = None, db: AsyncSession = Depends(get_db),
                         _: User = Depends(current_user)):
    q = select(CertEndpoint).order_by(CertEndpoint.host, CertEndpoint.port)
    if customer_id:
        q = q.where(CertEndpoint.customer_id == customer_id)
    names = await certs.node_names(db)
    cust = {e.id: e.name for e in (await db.execute(select(Environment))).scalars()}
    return [{**certs.endpoint_out(e, names), "customer_name": cust.get(e.customer_id)}
            for e in (await db.execute(q)).scalars()]


class EndpointIn(BaseModel):
    customer_id: str
    host: str = Field(min_length=1, max_length=300)
    port: int = 443
    name: str | None = Field(default=None, max_length=120)
    sni: str | None = Field(default=None, max_length=255)
    starttls: str = "none"
    probe_node_id: str | None = None
    warn_days: int | None = None
    enabled: bool = True


async def _save_ep(db: AsyncSession, ep: CertEndpoint | None, body: EndpointIn, admin: User, request: Request):
    try:
        ep = await certs.save_endpoint(db, ep, by=admin.username, **body.model_dump())
    except certs.CertError as e:
        raise _bad(e) from e
    await audit(db, "netsec.endpoint_save", user=admin, target=certs.endpoint_label(ep), ip=client_ip(request))
    await db.commit()
    agent_hub.spawn(certs.check_endpoint(ep.id))
    return certs.endpoint_out(ep)


@router.post("/endpoints", status_code=201)
async def create_endpoint(body: EndpointIn, request: Request, db: AsyncSession = Depends(get_db),
                          admin: User = Depends(require_admin)):
    return await _save_ep(db, None, body, admin, request)


@router.put("/endpoints/{eid}")
async def update_endpoint(eid: str, body: EndpointIn, request: Request, db: AsyncSession = Depends(get_db),
                          admin: User = Depends(require_admin)):
    ep = await db.get(CertEndpoint, eid)
    if ep is None:
        raise _missing()
    return await _save_ep(db, ep, body, admin, request)


@router.delete("/endpoints/{eid}", status_code=204)
async def delete_endpoint(eid: str, request: Request, db: AsyncSession = Depends(get_db),
                          admin: User = Depends(require_admin)):
    ep = await db.get(CertEndpoint, eid)
    if ep is None:
        raise _missing()
    if ep.source == "uptime":
        raise _bad(certs.CertError("this address comes from an uptime check: remove or change the uptime check"))
    await audit(db, "netsec.endpoint_delete", user=admin, target=certs.endpoint_label(ep), ip=client_ip(request))
    await db.delete(ep)
    await db.commit()


@router.post("/endpoints/{eid}/check")
async def check_endpoint(eid: str, db: AsyncSession = Depends(get_db), _: User = Depends(require_admin)):
    if await db.get(CertEndpoint, eid) is None:
        raise _missing()
    try:
        return await certs.check_endpoint(eid)
    except certs.CertError as e:
        raise _bad(e) from e


# ------------------------------------------------------------------ firewall
@router.get("/firewall/sources")
async def firewall_sources(customer_id: str | None = None, db: AsyncSession = Depends(get_db),
                           _: User = Depends(current_user)):
    return await netsec.sources(db, customer_id)


class SourcePatch(BaseModel):
    enabled: bool


@router.patch("/firewall/sources/{sid}")
async def patch_source(sid: str, body: SourcePatch, request: Request, db: AsyncSession = Depends(get_db),
                       admin: User = Depends(require_admin)):
    s = await db.get(FirewallSource, sid)
    if s is None:
        raise _missing()
    s.enabled = body.enabled
    await audit(db, "netsec.fw_source", user=admin, target=s.label, ip=client_ip(request), enabled=body.enabled)
    await db.commit()
    return {"id": s.id, "enabled": s.enabled}


@router.delete("/firewall/sources/{sid}", status_code=204)
async def delete_source(sid: str, request: Request, db: AsyncSession = Depends(get_db),
                        admin: User = Depends(require_admin)):
    s = await db.get(FirewallSource, sid)
    if s is None:
        raise _missing()
    await audit(db, "netsec.fw_source_delete", user=admin, target=s.label, ip=client_ip(request))
    await db.delete(s)
    await db.commit()


@router.get("/firewall/changes")
async def firewall_changes(customer_id: str | None = None, source_id: str | None = None, unacknowledged: bool = False,
                           days: int = 30, db: AsyncSession = Depends(get_db), _: User = Depends(current_user)):
    return await netsec.changes(db, customer_id=customer_id, source_id=source_id, unacknowledged=unacknowledged,
                                days=days)


@router.get("/firewall/versions/{vid}")
async def firewall_version(vid: str, db: AsyncSession = Depends(get_db), _: User = Depends(current_user)):
    try:
        v, s = await netsec.find_version(db, vid)
    except netsec.NetsecError as e:
        raise _missing() from e
    return netsec.version_out(v, s, content=True)


@router.get("/firewall/sources/{sid}/current")
async def firewall_current(sid: str, db: AsyncSession = Depends(get_db), _: User = Depends(current_user)):
    s = await db.get(FirewallSource, sid)
    if s is None:
        raise _missing()
    v = (await db.execute(select(FirewallVersion).where(FirewallVersion.source_id == sid)
                          .order_by(FirewallVersion.taken_at.desc()).limit(1))).scalar_one_or_none()
    if v is None:
        raise _missing()
    return netsec.version_out(v, s, content=True)


class AckIn(BaseModel):
    version_id: str | None = None
    source_id: str | None = None
    customer_id: str | None = None
    note: str | None = Field(default=None, max_length=500)


@router.post("/firewall/ack")
async def acknowledge(body: AckIn, request: Request, db: AsyncSession = Depends(get_db),
                      admin: User = Depends(require_admin)):
    try:
        n = await netsec.acknowledge(db, version_id=body.version_id, source_id=body.source_id,
                                     customer_id=body.customer_id, by=admin.username, note=body.note)
    except netsec.NetsecError as e:
        raise _bad(e) from e
    await audit(db, "netsec.fw_ack", user=admin, target=body.version_id or body.source_id or body.customer_id,
                ip=client_ip(request), count=n)
    await db.commit()
    return {"acknowledged": n}


# ------------------------------------------------------------------ VPN
@router.get("/vpn")
async def vpn(customer_id: str | None = None, status_: str | None = Query(None, alias="status"), db: AsyncSession = Depends(get_db),
              _: User = Depends(current_user)):
    return await netsec.tunnels(db, customer_id, status_)


class WatchIn(BaseModel):
    watched: bool | None


@router.patch("/vpn/{tid}")
async def watch(tid: str, body: WatchIn, request: Request, db: AsyncSession = Depends(get_db),
                admin: User = Depends(require_admin)):
    t = await db.get(VpnTunnel, tid)
    if t is None:
        raise _missing()
    t.watched = body.watched
    await audit(db, "netsec.vpn_watch", user=admin, target=t.name, ip=client_ip(request), watched=body.watched)
    await db.commit()
    return netsec.tunnel_out(t)


@router.delete("/vpn/{tid}", status_code=204)
async def forget_tunnel(tid: str, request: Request, db: AsyncSession = Depends(get_db),
                        admin: User = Depends(require_admin)):
    t = await db.get(VpnTunnel, tid)
    if t is None:
        raise _missing()
    await audit(db, "netsec.vpn_forget", user=admin, target=t.name, ip=client_ip(request))
    await db.delete(t)
    await db.commit()


# ------------------------------------------------------------------ settings
class SettingsIn(BaseModel):
    cert_interval_hours: int = Field(ge=1, le=168)
    cert_warn_days: int = Field(ge=1, le=180)
    cert_critical_days: int = Field(ge=0, le=60)
    alert_certs: bool = True
    alert_untrusted: bool = True
    uptime_endpoints: bool = True
    extra_paths: list[str] = Field(default_factory=list, max_length=50)
    fw_interval_minutes: int = Field(ge=5, le=1440)
    fw_keep_versions: int = Field(ge=5, le=500)
    alert_fw_changes: bool = True
    auto_ack_own: bool = True
    fw_ignore: list[str] = Field(default_factory=list, max_length=50)
    vpn_interval_minutes: int = Field(ge=1, le=120)
    vpn_down_minutes: int = Field(ge=1, le=1440)
    alert_vpn: bool = True
    wg_handshake_seconds: int = Field(ge=150, le=3600)

    @field_validator("extra_paths")
    @classmethod
    def _paths(cls, v: list[str]) -> list[str]:
        try:
            return certs.clean_paths(v)
        except certs.CertError as e:
            raise ValueError(str(e)) from e

    @field_validator("fw_ignore")
    @classmethod
    def _regex(cls, v: list[str]) -> list[str]:
        out = []
        for p in v:
            p = p.strip()
            if not p:
                continue
            try:
                re.compile(p)
            except re.error as e:
                raise ValueError(f"'{p}' is not a valid regular expression: {e}") from e
            out.append(p[:200])
        return out


@router.get("/settings")
async def get_settings(db: AsyncSession = Depends(get_db), _: User = Depends(current_user)) -> dict[str, Any]:
    return await get_setting(db, "netsec")


@router.put("/settings")
async def put_settings(body: SettingsIn, request: Request, db: AsyncSession = Depends(get_db),
                       admin: User = Depends(require_admin)) -> dict[str, Any]:
    if body.cert_critical_days >= body.cert_warn_days:
        raise _bad(ValueError("the critical limit must be lower than the warning"))
    cur = await get_setting(db, "netsec")
    out = await set_setting(db, "netsec", {**cur, **body.model_dump()})
    await audit(db, "netsec.settings", user=admin, ip=client_ip(request))
    await db.commit()
    return out
