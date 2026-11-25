"""Microsoft 365 and Entra ID per customer (phase 19)."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from mmn.api.deps import client_ip, current_user, require_admin
from mmn.db import get_db
from mmn.models import Environment, M365Tenant, User, VaultItem
from mmn.services import agent_hub, m365
from mmn.services.audit import audit

router = APIRouter(prefix="/api/m365", tags=["m365"])

_STATUS = {"not_found": 404, "invalid": 400, "ambiguous": 400, "no_credential": 400, "bad_credential": 400,
           "busy": 409, "no_permission": 403, "auth_failed": 502, "unreachable": 502}


def _err(e: m365.M365Error) -> HTTPException:
    return HTTPException(_STATUS.get(e.code, 502), detail=str(e))


async def _tenant(db: AsyncSession, tid: str) -> M365Tenant:
    t = await db.get(M365Tenant, tid)
    if t is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="tenant not found")
    return t


async def _out(db: AsyncSession, t: M365Tenant) -> dict[str, Any]:
    names = await m365.vault_names(db, [t.vault_item_id])
    return m365.tenant_out(t, vault_name=names.get(t.vault_item_id or ""))


async def _vault_item(db: AsyncSession, customer_id: str, item_id: str) -> VaultItem:
    item = await db.get(VaultItem, item_id)
    if item is None or item.customer_id != customer_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="the vault item does not belong to this customer")
    return item


# ------------------------------------------------------------------ setup help
@router.get("/permissions")
async def permissions(_: User = Depends(current_user)):
    """The Microsoft Graph application permissions the app registration needs."""
    return {"read": [{"permission": p, "for": w} for p, w in m365.READ_PERMISSIONS],
            "write": [{"permission": p, "for": w} for p, w in m365.WRITE_PERMISSIONS],
            "actions": list(m365.ACTIONS), "defaults": m365.DEFAULT_SETTINGS}


# ------------------------------------------------------------------ tenants
class TenantIn(BaseModel):
    customer_id: str
    name: str = Field(min_length=1, max_length=120)
    tenant_id: str = Field(min_length=3, max_length=100)
    client_id: str = Field(min_length=36, max_length=64)
    auth_method: Literal["secret", "certificate"] = "secret"
    vault_item_id: str | None = None  # an existing vault item with the secret or certificate
    client_secret: str | None = Field(default=None, max_length=2000)  # or: a new secret, stored in the vault
    certificate_pem: str | None = Field(default=None, max_length=20000)  # or: key + certificate (PEM)
    generate_certificate: bool = False  # or: Stackpilot makes a certificate (download it for Entra)
    enabled: bool = True
    settings: dict[str, Any] | None = None


class TenantPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    tenant_id: str | None = Field(default=None, min_length=3, max_length=100)
    client_id: str | None = Field(default=None, min_length=36, max_length=64)
    auth_method: Literal["secret", "certificate"] | None = None
    vault_item_id: str | None = None
    client_secret: str | None = Field(default=None, max_length=2000)
    certificate_pem: str | None = Field(default=None, max_length=20000)
    generate_certificate: bool = False
    enabled: bool | None = None
    settings: dict[str, Any] | None = None


async def _apply_credential(db: AsyncSession, t: M365Tenant, body: TenantIn | TenantPatch, by: str) -> dict[str, Any]:
    """Store or pick the credential; returns {"certificate": PEM} when Stackpilot made one."""
    out: dict[str, Any] = {}
    current = await db.get(VaultItem, t.vault_item_id) if t.vault_item_id else None
    # Only update the current item in place when it is the tenant's own item of the same kind.
    reuse = current if current is not None and "m365" in (current.tags or []) and current.kind == (
        "api_key" if t.auth_method == "secret" else "certificate") else None
    if body.vault_item_id:
        await _vault_item(db, t.customer_id, body.vault_item_id)
        t.vault_item_id = body.vault_item_id
    elif t.auth_method == "secret" and body.client_secret and body.client_secret.strip():
        item = await m365.store_credential(db, customer_id=t.customer_id, name=t.name, auth_method="secret",
                                           client_id=t.client_id, tenant=t.tenant_id, secret=body.client_secret,
                                           item=reuse, by=by)
        t.vault_item_id = item.id
    elif t.auth_method == "certificate" and (body.generate_certificate or (body.certificate_pem or "").strip()):
        if body.generate_certificate:
            pem, cert_pem, info = m365.generate_certificate(t.name)
            out["certificate"] = cert_pem
        else:
            pem = body.certificate_pem or ""
            try:
                _, cert = m365.parse_certificate({"password": pem})
            except m365.M365Error as e:
                raise _err(e) from e
            info = m365.cert_info(cert)
        item = await m365.store_credential(db, customer_id=t.customer_id, name=t.name, auth_method="certificate",
                                           client_id=t.client_id, tenant=t.tenant_id, pem=pem, info=info,
                                           item=reuse, by=by)
        t.vault_item_id = item.id
        out["certificate_info"] = info
    m365.forget_token(t.id)
    return out


@router.get("/tenants")
async def list_tenants(customer_id: str | None = None, db: AsyncSession = Depends(get_db),
                       _: User = Depends(current_user)):
    rows = await m365.tenants_of(db, customer_id)
    names = await m365.vault_names(db, [t.vault_item_id for t in rows])
    return [m365.tenant_out(t, vault_name=names.get(t.vault_item_id or "")) for t in rows]


@router.post("/tenants", status_code=201)
async def create_tenant(body: TenantIn, request: Request, db: AsyncSession = Depends(get_db),
                        admin: User = Depends(require_admin)):
    if await db.get(Environment, body.customer_id) is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="customer not found")
    try:
        tid, cid = m365.check_ids(body.tenant_id, body.client_id)
        settings = m365.clean_settings(body.settings)
    except m365.M365Error as e:
        raise _err(e) from e
    t = M365Tenant(customer_id=body.customer_id, name=body.name.strip(), tenant_id=tid, client_id=cid,
                   auth_method=body.auth_method, enabled=body.enabled, settings=settings, snapshot={},
                   created_by=admin.username, updated_by=admin.username)
    db.add(t)
    await db.flush()
    extra = await _apply_credential(db, t, body, admin.username)
    if not t.vault_item_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="give the client secret or certificate, or choose a vault item")
    await audit(db, "m365.tenant.create", user=admin, target=t.name, ip=client_ip(request), tenant=tid, client=cid,
                auth=t.auth_method)
    await db.commit()
    await db.refresh(t)
    if t.enabled:
        agent_hub.spawn(_sync_quiet(t.id))
    return {**await _out(db, t), **extra}


async def _sync_quiet(tid: str) -> None:
    try:
        await m365.sync(tid)
    except m365.M365Error:
        pass


@router.patch("/tenants/{tid}")
async def update_tenant(tid: str, body: TenantPatch, request: Request, db: AsyncSession = Depends(get_db),
                        admin: User = Depends(require_admin)):
    t = await _tenant(db, tid)
    data = body.model_dump(exclude_unset=True)
    try:
        if "tenant_id" in data or "client_id" in data:
            t.tenant_id, t.client_id = m365.check_ids(data.get("tenant_id") or t.tenant_id,
                                                      data.get("client_id") or t.client_id)
        if body.settings is not None:
            t.settings = {**(t.settings or {}), **m365.clean_settings(body.settings)}
    except m365.M365Error as e:
        raise _err(e) from e
    if body.name:
        t.name = body.name.strip()
    if body.enabled is not None:
        t.enabled = body.enabled
    if body.auth_method and body.auth_method != t.auth_method:
        t.auth_method = body.auth_method
        if not (body.vault_item_id or body.client_secret or body.certificate_pem or body.generate_certificate):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="a new sign-in method needs its secret or certificate")
    extra = await _apply_credential(db, t, body, admin.username)
    from datetime import UTC, datetime

    t.updated_at, t.updated_by = datetime.now(UTC), admin.username
    await audit(db, "m365.tenant.update", user=admin, target=t.name, ip=client_ip(request),
                fields=sorted(k for k in data if k not in ("client_secret", "certificate_pem")),
                credential_changed=bool(body.client_secret or body.certificate_pem or body.generate_certificate
                                        or body.vault_item_id) or None)
    await db.commit()
    await db.refresh(t)
    return {**await _out(db, t), **extra}


@router.delete("/tenants/{tid}", status_code=204)
async def delete_tenant(tid: str, request: Request, db: AsyncSession = Depends(get_db),
                        admin: User = Depends(require_admin)):
    t = await _tenant(db, tid)
    name = t.name
    await db.delete(t)
    m365.forget_token(tid)
    await audit(db, "m365.tenant.delete", user=admin, target=name, ip=client_ip(request))
    await db.commit()
    return Response(status_code=204)


@router.post("/tenants/{tid}/test")
async def test_tenant(tid: str, request: Request, db: AsyncSession = Depends(get_db),
                      admin: User = Depends(require_admin)):
    t = await _tenant(db, tid)
    try:
        res = await m365.test(db, t, username=admin.username)
        ok, err = True, None
    except m365.M365Error as e:
        res, ok, err = {"error": str(e), "code": e.code}, False, str(e)
    await audit(db, "m365.tenant.test", user=admin, target=t.name, ip=client_ip(request), success=ok, error=err)
    await db.commit()
    return {"ok": ok, **res}


@router.post("/tenants/{tid}/sync")
async def sync_tenant(tid: str, db: AsyncSession = Depends(get_db), _: User = Depends(current_user)):
    t = await _tenant(db, tid)
    try:
        res = await m365.sync(t.id)
    except m365.M365Error as e:
        raise _err(e) from e
    await db.refresh(t)
    return {**res, "tenant": await _out(db, t)}


@router.get("/tenants/{tid}/certificate")
async def download_certificate(tid: str, db: AsyncSession = Depends(get_db), _: User = Depends(require_admin)):
    """The public certificate (no key) to upload in the app registration in Entra."""
    from mmn.services import vault

    t = await _tenant(db, tid)
    if t.auth_method != "certificate" or not t.vault_item_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="this tenant does not sign in with a certificate")
    item = await db.get(VaultItem, t.vault_item_id)
    if item is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="the vault item is gone")
    try:
        pem = m365.public_certificate(vault.read_secret(item))
    except m365.M365Error as e:
        raise _err(e) from e
    fname = "".join(c if c.isalnum() or c in "-_" else "-" for c in t.name)[:60] or "stackpilot"
    return Response(pem, media_type="application/x-pem-file",
                    headers={"Content-Disposition": f'attachment; filename="stackpilot-{fname}.cer"'})


# ------------------------------------------------------------------ data
@router.get("/overview")
async def overview(customer_id: str | None = None, db: AsyncSession = Depends(get_db), _: User = Depends(current_user)):
    rows = await m365.tenants_of(db, customer_id)
    names = await m365.vault_names(db, [t.vault_item_id for t in rows])
    tenants = [m365.tenant_out(t, vault_name=names.get(t.vault_item_id or "")) for t in rows]
    totals: dict[str, int] = {}
    for x in tenants:
        for k, v in (x["counts"] or {}).items():
            if isinstance(v, int):
                totals[k] = totals.get(k, 0) + v
    failed = await m365.failed_counts(db, [t.id for t in rows])
    foreign = await m365.foreign_success(db, [t.id for t in rows])
    return {"tenants": tenants, "totals": totals,
            "failed_24h": sum(failed.values()), "foreign_24h": len(foreign),
            "risk_detections": sorted(
                [{**d, "tenant_id": t.id, "tenant": t.name, "customer_id": t.customer_id}
                 for t in rows for d in (t.snapshot or {}).get("risk_detections") or []],
                key=lambda d: d.get("at") or "", reverse=True)[:50]}


@router.get("/users")
async def users(customer_id: str | None = None, tenant_id: str | None = None, filter: str = "all",
                q: str | None = Query(default=None, max_length=200), limit: int = Query(default=1000, le=5000),
                db: AsyncSession = Depends(get_db), _: User = Depends(current_user)):
    rows = [await _tenant(db, tenant_id)] if tenant_id else await m365.tenants_of(db, customer_id)
    out: list[dict[str, Any]] = []
    try:
        for t in rows:
            out += [{**u, "tenant_id": t.id, "tenant": t.name, "customer_id": t.customer_id}
                    for u in m365.filter_users(t, filter, q)]
    except m365.M365Error as e:
        raise _err(e) from e
    return {"users": out[:limit], "total": len(out), "filters": list(m365.USER_FILTERS)}


@router.get("/users/{tid}/{uid}")
async def user_detail(tid: str, uid: str, db: AsyncSession = Depends(get_db), _: User = Depends(current_user)):
    t = await _tenant(db, tid)
    u = m365.find_user(t, uid)
    if u is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="user not found")
    snap = t.snapshot or {}
    return {**u, "tenant_id": t.id, "tenant": t.name, "customer_id": t.customer_id,
            "signins": await m365.signins(db, tenant_id=t.id, user=u.get("upn") or u["id"], hours=24 * 30, limit=100),
            "risk_detections": [d for d in snap.get("risk_detections") or [] if d.get("user_id") == u["id"]],
            "actions": m365.permissions_view(snap.get("granted") or [])["actions"]}


@router.get("/licences")
async def licences(customer_id: str | None = None, db: AsyncSession = Depends(get_db), _: User = Depends(current_user)):
    out = []
    for t in await m365.tenants_of(db, customer_id):
        for lic in (t.snapshot or {}).get("licences") or []:
            out.append({**lic, "tenant_id": t.id, "tenant": t.name, "customer_id": t.customer_id,
                        "customer_name": t.customer.name if t.customer else None})
    return {"licences": out}


@router.get("/signins")
async def signins(customer_id: str | None = None, tenant_id: str | None = None, flag: str | None = None,
                  user: str | None = None, hours: int = Query(default=168, ge=1, le=2160),
                  limit: int = Query(default=300, le=1000), db: AsyncSession = Depends(get_db),
                  _: User = Depends(current_user)):
    return {"signins": await m365.signins(db, customer_id=customer_id, tenant_id=tenant_id, user=user, flag=flag,
                                          hours=hours, limit=limit)}


class ActionIn(BaseModel):
    action: Literal["block", "unblock", "revoke_sessions", "reset_mfa", "reset_password", "confirm_compromised",
                    "dismiss_risk"]
    reason: str | None = Field(default=None, max_length=1000)


@router.post("/users/{tid}/{uid}/action")
async def user_action(tid: str, uid: str, body: ActionIn, request: Request, db: AsyncSession = Depends(get_db),
                      admin: User = Depends(require_admin)):
    t = await _tenant(db, tid)
    try:
        res = await m365.user_action(t, uid, body.action, by=admin.username, reason=body.reason)
        ok, err = True, None
    except m365.M365Error as e:
        await audit(db, f"m365.user.{body.action}", user=admin, target=uid, ip=client_ip(request), success=False,
                    error=str(e), tenant=t.name)
        await db.commit()
        raise _err(e) from e
    await audit(db, f"m365.user.{body.action}", user=admin, target=res.get("user") or uid, ip=client_ip(request),
                success=ok, error=err, tenant=t.name, reason=body.reason)
    await db.commit()
    return res
