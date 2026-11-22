"""UniFi Network, Site Manager and Access (phase 6)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from mmn.api.deps import client_ip, current_user, require_admin
from mmn.db import get_db
from mmn.models import User
from mmn.services.audit import audit
from mmn.services.unifi import service as uf
from mmn.services.unifi.transport import UnifiError, fetch_certificate

router = APIRouter(prefix="/api/unifi", tags=["unifi"])

_BAD_REQUEST = {"bad_url", "bad_transport", "no_agent", "no_cloud", "no_console", "bad_fingerprint", "no_key",
                "duplicate", "invalid", "wrong_kind", "site", "ambiguous", "no_site", "not_supported"}


def _err(e: UnifiError) -> HTTPException:
    if e.code == "not_found" and e.status is None:
        return HTTPException(status.HTTP_404_NOT_FOUND, detail=str(e))
    if e.code in ("read_only",):
        return HTTPException(status.HTTP_403_FORBIDDEN, detail=str(e))
    if e.code in _BAD_REQUEST:
        return HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(e))
    return HTTPException(status.HTTP_502_BAD_GATEWAY, detail=str(e))


async def _conn(db: AsyncSession, cid: str, kind: str | tuple[str, ...] | None = None):
    try:
        return await uf.get_connection(db, cid, kind)
    except UnifiError as e:
        raise _err(e) from e


# ------------------------------------------------------------------ connections
@router.get("/connections")
async def list_connections(environment_id: str | None = None, kind: str | None = None,
                           db: AsyncSession = Depends(get_db), _: User = Depends(current_user)):
    return [uf.connection_out(c) for c in await uf.list_connections(db, environment_id, kind)]


@router.post("/connections", status_code=201)
async def create_connection(body: uf.ConnectionIn, request: Request, db: AsyncSession = Depends(get_db),
                            admin: User = Depends(require_admin)):
    try:
        c = await uf.create_connection(db, body)
    except UnifiError as e:
        raise _err(e) from e
    await audit(db, "unifi.connection.create", user=admin, target=c.name, ip=client_ip(request), kind=c.kind,
                transport=c.transport, read_only=c.read_only, tls=c.tls_mode)
    await db.commit()
    await db.refresh(c)
    try:
        await uf.poll(c.id)
    except UnifiError:
        pass
    await db.refresh(c)
    return uf.connection_out(c)


@router.patch("/connections/{cid}")
async def update_connection(cid: str, body: uf.ConnectionPatch, request: Request, db: AsyncSession = Depends(get_db),
                            admin: User = Depends(require_admin)):
    c = await _conn(db, cid)
    try:
        await uf.update_connection(db, c, body)
    except UnifiError as e:
        raise _err(e) from e
    await audit(db, "unifi.connection.update", user=admin, target=c.name, ip=client_ip(request),
                fields=sorted(body.model_dump(exclude_unset=True, exclude={"api_key"})),
                key_changed=bool(body.api_key) or None)
    await db.commit()
    return uf.connection_out(c)


@router.delete("/connections/{cid}", status_code=204)
async def delete_connection(cid: str, request: Request, db: AsyncSession = Depends(get_db),
                            admin: User = Depends(require_admin)):
    c = await _conn(db, cid)
    name = c.name
    await db.delete(c)
    await audit(db, "unifi.connection.delete", user=admin, target=name, ip=client_ip(request))
    await db.commit()
    return Response(status_code=204)


@router.post("/connections/{cid}/test")
async def test_connection(cid: str, request: Request, db: AsyncSession = Depends(get_db),
                          admin: User = Depends(require_admin)):
    c = await _conn(db, cid)
    try:
        res = await uf.test_connection(db, c)
        ok, err = True, None
    except UnifiError as e:
        res, ok, err = {"error": str(e), "code": e.code}, False, str(e)
    await audit(db, "unifi.connection.test", user=admin, target=c.name, ip=client_ip(request), success=ok, error=err)
    await db.commit()
    return {"ok": ok, **res}


@router.post("/connections/{cid}/refresh")
async def refresh_connection(cid: str, db: AsyncSession = Depends(get_db), _: User = Depends(current_user)):
    c = await _conn(db, cid)
    try:
        await uf.poll(c.id)
    except UnifiError as e:
        raise _err(e) from e
    await db.refresh(c)
    return uf.connection_out(c)


class CertIn(BaseModel):
    url: str = Field(min_length=1, max_length=500)
    kind: Literal["network", "access"] = "network"
    agent_node_id: str | None = None


@router.post("/certificate")
async def certificate(body: CertIn, _: User = Depends(require_admin)):
    try:
        url = uf.default_url(body.kind, body.url) or body.url
        return await fetch_certificate(url, agent_node_id=body.agent_node_id or None)
    except UnifiError as e:
        raise _err(e) from e


@router.get("/events")
async def events(connection_id: str | None = None, since_hours: int = Query(168, ge=1, le=24 * 90),
                 kind: str | None = None, limit: int = Query(200, ge=1, le=1000),
                 db: AsyncSession = Depends(get_db), _: User = Depends(current_user)):
    return await uf.list_events(db, connection_id, since_hours, kind, limit)


# ------------------------------------------------------------------ network
@router.get("/network/overview")
async def network_overview(environment_id: str | None = None, connection_id: str | None = None,
                           db: AsyncSession = Depends(get_db), _: User = Depends(current_user)):
    return await uf.network_overview(db, environment_id, connection_id)


@router.get("/network/firmware")
async def firmware(environment_id: str | None = None, db: AsyncSession = Depends(get_db),
                   _: User = Depends(current_user)):
    return uf.firmware_report(await uf.list_connections(db, environment_id))


@router.get("/network/{cid}/sites/{sid}/devices/{did}")
async def device(cid: str, sid: str, did: str, db: AsyncSession = Depends(get_db), _: User = Depends(current_user)):
    c = await _conn(db, cid, "network")
    try:
        return await uf.device_detail(db, c, sid, did)
    except UnifiError as e:
        raise _err(e) from e


@router.get("/network/{cid}/sites/{sid}/clients")
async def clients(cid: str, sid: str, q: str | None = None, limit: int = Query(500, ge=1, le=5000),
                  db: AsyncSession = Depends(get_db), _: User = Depends(current_user)):
    c = await _conn(db, cid, "network")
    try:
        return await uf.clients(db, c, sid, q, limit)
    except UnifiError as e:
        raise _err(e) from e


@router.get("/network/{cid}/sites/{sid}/wifi")
async def wifi(cid: str, sid: str, db: AsyncSession = Depends(get_db), _: User = Depends(current_user)):
    c = await _conn(db, cid, "network")
    try:
        return {"wifi": await uf.live_wifi(db, c, sid)}
    except UnifiError as e:
        raise _err(e) from e


@router.get("/network/{cid}/sites/{sid}/diagnose-client")
async def diagnose_client(cid: str, sid: str, q: str = Query(min_length=1, max_length=100),
                          db: AsyncSession = Depends(get_db), _: User = Depends(current_user)):
    c = await _conn(db, cid, "network")
    try:
        return await uf.diagnose_client(db, c, sid, q)
    except UnifiError as e:
        raise _err(e) from e


@router.get("/network/{cid}/sites/{sid}/guest-isolation")
async def guest_isolation(cid: str, sid: str, db: AsyncSession = Depends(get_db), _: User = Depends(current_user)):
    c = await _conn(db, cid, "network")
    try:
        return await uf.guest_isolation(db, c, sid)
    except UnifiError as e:
        raise _err(e) from e


class ReasonIn(BaseModel):
    reason: str | None = Field(default=None, max_length=500)


async def _network_action(db, request, admin, cid: str, action: str, target: str, fn, **detail):
    c = await _conn(db, cid, "network")
    try:
        uf.require_writable(c)
        res = await fn(await uf.network_api(db, c))
    except UnifiError as e:
        await audit(db, f"unifi.{action}", user=admin, target=f"{c.name}: {target}", ip=client_ip(request),
                    success=False, error=str(e), **detail)
        await db.commit()
        raise _err(e) from e
    await audit(db, f"unifi.{action}", user=admin, target=f"{c.name}: {target}", ip=client_ip(request), **detail)
    await db.commit()
    return {"ok": True, "result": res}


@router.post("/network/{cid}/sites/{sid}/devices/{did}/restart")
async def restart_device(cid: str, sid: str, did: str, body: ReasonIn, request: Request,
                         db: AsyncSession = Depends(get_db), admin: User = Depends(require_admin)):
    return await _network_action(db, request, admin, cid, "device.restart", did,
                                 lambda api: api.device_action(sid, did, "RESTART"), site=sid, reason=body.reason)


@router.post("/network/{cid}/sites/{sid}/devices/{did}/ports/{idx}/power-cycle")
async def power_cycle(cid: str, sid: str, did: str, idx: int, body: ReasonIn, request: Request,
                      db: AsyncSession = Depends(get_db), admin: User = Depends(require_admin)):
    return await _network_action(db, request, admin, cid, "port.power_cycle", f"{did} port {idx}",
                                 lambda api: api.port_action(sid, did, idx, "POWER_CYCLE"), site=sid, reason=body.reason)


class GuestIn(BaseModel):
    authorize: bool


@router.post("/network/{cid}/sites/{sid}/clients/{client_id}/guest")
async def guest_access(cid: str, sid: str, client_id: str, body: GuestIn, request: Request,
                       db: AsyncSession = Depends(get_db), admin: User = Depends(require_admin)):
    action = "AUTHORIZE_GUEST_ACCESS" if body.authorize else "UNAUTHORIZE_GUEST_ACCESS"
    return await _network_action(db, request, admin, cid, "client.guest", client_id,
                                 lambda api: api.client_action(sid, client_id, action), site=sid, action=action)


class WifiEnabledIn(BaseModel):
    enabled: bool


async def set_wifi(api, sid: str, wid: str, enabled: bool):
    cur = await api.wifi_one(sid, wid)
    body = {k: v for k, v in cur.items() if k not in ("id", "metadata")}
    body["enabled"] = enabled
    await api.update_wifi(sid, wid, body)
    return {"enabled": enabled}


@router.post("/network/{cid}/sites/{sid}/wifi/{wid}/enabled")
async def wifi_enabled(cid: str, sid: str, wid: str, body: WifiEnabledIn, request: Request,
                       db: AsyncSession = Depends(get_db), admin: User = Depends(require_admin)):
    return await _network_action(db, request, admin, cid, "wifi.enabled", wid,
                                 lambda api: set_wifi(api, sid, wid, body.enabled), site=sid, enabled=body.enabled)


# ------------------------------------------------------------------ site manager
@router.get("/cloud/{cid}")
async def cloud(cid: str, db: AsyncSession = Depends(get_db), _: User = Depends(current_user)):
    c = await _conn(db, cid, "site_manager")
    return {"connection": uf.connection_out(c), **(c.snapshot or {})}


@router.get("/cloud/{cid}/isp-metrics")
async def isp_metrics(cid: str, duration: Literal["24h", "7d", "30d"] = "7d", db: AsyncSession = Depends(get_db),
                      _: User = Depends(current_user)):
    c = await _conn(db, cid, "site_manager")
    try:
        api = await uf.cloud_api(db, c)
        return await api.isp_metrics("5m" if duration == "24h" else "1h", duration)
    except UnifiError as e:
        raise _err(e) from e


# ------------------------------------------------------------------ access
@router.get("/access/overview")
async def access_overview(environment_id: str | None = None, db: AsyncSession = Depends(get_db),
                          _: User = Depends(current_user)):
    out = []
    for c in await uf.list_connections(db, environment_id, "access"):
        out.append({**uf.connection_out(c), "doors": (c.snapshot or {}).get("doors") or []})
    return out


@router.get("/access/{cid}/doors")
async def doors(cid: str, db: AsyncSession = Depends(get_db), _: User = Depends(current_user)):
    c = await _conn(db, cid, "access")
    try:
        snap = await uf.poll(c.id)
    except UnifiError as e:
        raise _err(e) from e
    return snap["doors"]


@router.get("/access/{cid}/doors/{did}/lock-rule")
async def get_lock_rule(cid: str, did: str, db: AsyncSession = Depends(get_db), _: User = Depends(current_user)):
    c = await _conn(db, cid, "access")
    try:
        return await (await uf.access_api(db, c)).lock_rule(did)
    except UnifiError as e:
        raise _err(e) from e


@router.get("/access/{cid}/logs")
async def access_logs(cid: str, hours: float = Query(24, gt=0, le=24 * 90), since: datetime | None = None,
                      until: datetime | None = None, topic: str = "door_openings", door: str | None = None,
                      actor: str | None = None, limit: int = Query(200, ge=1, le=500),
                      db: AsyncSession = Depends(get_db), _: User = Depends(current_user)):
    c = await _conn(db, cid, "access")
    try:
        return await uf.access_log(db, c, hours=hours, since=since, until=until, topic=topic, door=door, actor=actor,
                                   limit=limit)
    except UnifiError as e:
        raise _err(e) from e


@router.get("/access/{cid}/schedules")
async def schedules(cid: str, db: AsyncSession = Depends(get_db), _: User = Depends(current_user)):
    c = await _conn(db, cid, "access")
    try:
        api = await uf.access_api(db, c)
        return {"schedules": await api.schedules(), "policies": await uf._optional(api.policies()) or []}
    except UnifiError as e:
        raise _err(e) from e


async def _door_action(db, request, admin, cid: str, did: str, action: str, fn, **detail):
    c = await _conn(db, cid, "access")
    try:
        uf.require_writable(c)
        res = await fn(await uf.access_api(db, c))
    except UnifiError as e:
        await audit(db, f"unifi.door.{action}", user=admin, target=f"{c.name}: {did}", ip=client_ip(request),
                    success=False, error=str(e), **detail)
        await db.commit()
        raise _err(e) from e
    await audit(db, f"unifi.door.{action}", user=admin, target=f"{c.name}: {did}", ip=client_ip(request), **detail)
    await db.commit()
    return {"ok": True, "result": res}


@router.post("/access/{cid}/doors/{did}/unlock")
async def unlock(cid: str, did: str, body: ReasonIn, request: Request, db: AsyncSession = Depends(get_db),
                 admin: User = Depends(require_admin)):
    return await _door_action(db, request, admin, cid, did, "unlock",
                              lambda api: api.unlock(did, f"{admin.username} (Stackpilot)"), reason=body.reason)


class LockRuleIn(BaseModel):
    type: Literal["keep_lock", "keep_unlock", "custom", "reset", "lock_early"]
    interval: int | None = Field(default=None, ge=1, le=24 * 60)
    reason: str | None = Field(default=None, max_length=500)


@router.put("/access/{cid}/doors/{did}/lock-rule")
async def set_lock_rule(cid: str, did: str, body: LockRuleIn, request: Request, db: AsyncSession = Depends(get_db),
                        admin: User = Depends(require_admin)):
    return await _door_action(db, request, admin, cid, did, "lock_rule",
                              lambda api: api.set_lock_rule(did, body.type, body.interval), rule=body.type,
                              interval=body.interval, reason=body.reason)


@router.get("/access/{cid}/emergency")
async def get_emergency(cid: str, db: AsyncSession = Depends(get_db), _: User = Depends(current_user)):
    c = await _conn(db, cid, "access")
    try:
        return await (await uf.access_api(db, c)).emergency()
    except UnifiError as e:
        raise _err(e) from e


class EmergencyIn(BaseModel):
    lockdown: bool = False
    evacuation: bool = False
    confirm: str


@router.put("/access/{cid}/emergency")
async def set_emergency(cid: str, body: EmergencyIn, request: Request, db: AsyncSession = Depends(get_db),
                        admin: User = Depends(require_admin)):
    c = await _conn(db, cid, "access")
    if body.confirm != c.name:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="confirm must equal the connection name")
    return await _door_action(db, request, admin, cid, "all doors", "emergency",
                              lambda api: api.set_emergency(body.lockdown, body.evacuation), lockdown=body.lockdown,
                              evacuation=body.evacuation)
