"""The password vault (phase 18): credentials per customer, encrypted at rest, with an access log of every
reveal, copy and AI use."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from mmn.api.deps import client_ip, current_user, get_session, require_admin
from mmn.api.ratelimit import RateLimiter
from mmn.db import get_db
from mmn.models import BackupTarget, Role, User, UserSession, VaultItem, VaultItemHistory
from mmn.security import verify_password
from mmn.services import events, vault
from mmn.services.audit import audit
from mmn.services.settings_store import get_setting, set_setting

router = APIRouter(prefix="/api/vault", tags=["vault"])
# Unlock attempts: at most 10 per 15 minutes per admin.
unlock_limiter = RateLimiter(limit=10, window_seconds=15 * 60)


def _bad(e: Exception) -> HTTPException:
    return HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(e))


def _who(u: User) -> str:
    return u.full_name or u.username


async def _item(db: AsyncSession, iid: str) -> VaultItem:
    item = await db.get(VaultItem, iid)
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="not_found")
    return item


async def _unlocked(db: AsyncSession, sess: UserSession | None, user: User) -> None:
    """Secrets are for admins in an unlocked session (password entered again recently)."""
    if user.role != Role.admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="admin_required")
    cfg = await get_setting(db, "vault")
    if not cfg.get("require_unlock", True):
        return
    if sess is None or sess.vault_unlocked_until is None or sess.vault_unlocked_until < datetime.now(UTC):
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="vault_locked")


# ------------------------------------------------------------------ status, unlock, settings
@router.get("/status")
async def vault_status(db: AsyncSession = Depends(get_db), sess: UserSession | None = Depends(get_session),
                       user: User = Depends(current_user)):
    cfg = await get_setting(db, "vault")
    until = sess.vault_unlocked_until if sess else None
    unlocked = user.role == Role.admin and (not cfg.get("require_unlock", True)
                                            or bool(until and until > datetime.now(UTC)))
    plain = (await db.execute(select(func.count()).select_from(BackupTarget)
                              .where(BackupTarget.passphrase_enc.is_(None)))).scalar_one()
    return {"unlocked": unlocked, "unlocked_until": until if unlocked else None,
            "require_unlock": cfg.get("require_unlock", True), "can_reveal": user.role == Role.admin,
            "key": vault.key_info() if user.role == Role.admin else None,
            "unencrypted_backup_targets": plain, "grants": len(await vault.active_grants(db))}


class UnlockIn(BaseModel):
    password: str = Field(min_length=1, max_length=1024)


@router.post("/unlock")
async def unlock(body: UnlockIn, request: Request, db: AsyncSession = Depends(get_db),
                 sess: UserSession | None = Depends(get_session), admin: User = Depends(require_admin)):
    ip = client_ip(request)
    if not unlock_limiter.hit(admin.id):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, detail="too_many_attempts")
    if sess is None or not verify_password(admin.password_hash, body.password):
        await vault.log_access(db, None, "unlock", username=admin.username, ip=ip, success=False)
        await audit(db, "vault.unlock", user=admin, ip=ip, success=False)
        await db.commit()
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="wrong_password")
    cfg = await get_setting(db, "vault")
    minutes = max(1, min(int(cfg.get("unlock_minutes") or 15), 8 * 60))
    sess.vault_unlocked_until = datetime.now(UTC) + timedelta(minutes=minutes)
    await vault.log_access(db, None, "unlock", username=admin.username, ip=ip)
    await audit(db, "vault.unlock", user=admin, ip=ip, minutes=minutes)
    await db.commit()
    return {"unlocked": True, "unlocked_until": sess.vault_unlocked_until}


@router.post("/lock")
async def lock(db: AsyncSession = Depends(get_db), sess: UserSession | None = Depends(get_session),
               user: User = Depends(current_user)):
    if sess is not None:
        sess.vault_unlocked_until = None
        await db.commit()
    return {"unlocked": False}


class VaultSettings(BaseModel):
    unlock_minutes: int = Field(default=15, ge=1, le=480)
    require_unlock: bool = True
    grant_minutes: int = Field(default=15, ge=1, le=1440)
    grant_uses: int = Field(default=1, ge=1, le=20)
    history_keep: int = Field(default=10, ge=0, le=100)
    notify_ai_use: bool = True


@router.get("/settings")
async def get_vault_settings(db: AsyncSession = Depends(get_db), _: User = Depends(require_admin)):
    return await get_setting(db, "vault")


@router.put("/settings")
async def put_vault_settings(body: VaultSettings, request: Request, db: AsyncSession = Depends(get_db),
                             sess: UserSession | None = Depends(get_session), admin: User = Depends(require_admin)):
    await _unlocked(db, sess, admin)
    out = await set_setting(db, "vault", body.model_dump())
    await audit(db, "vault.settings", user=admin, ip=client_ip(request), **body.model_dump())
    await db.commit()
    return out


@router.post("/rotate-key")
async def rotate_key(request: Request, db: AsyncSession = Depends(get_db),
                     sess: UserSession | None = Depends(get_session), admin: User = Depends(require_admin)):
    await _unlocked(db, sess, admin)
    res = await vault.rotate_key(db)
    await vault.log_access(db, None, "rotate_key", username=admin.username, ip=client_ip(request), **res)
    await audit(db, "vault.rotate_key", user=admin, ip=client_ip(request), **res)
    await db.commit()
    return {**res, "key": vault.key_info()}


class GenerateIn(BaseModel):
    length: int = Field(default=24, ge=8, le=128)
    symbols: bool = True
    digits: bool = True
    avoid_ambiguous: bool = True
    words: int = Field(default=0, ge=0, le=12)


@router.post("/generate")
async def generate(body: GenerateIn, _: User = Depends(current_user)):
    return {"password": vault.generate_password(body.length, symbols=body.symbols, digits=body.digits,
                                                avoid_ambiguous=body.avoid_ambiguous, words=body.words)}


# ------------------------------------------------------------------ items
class Extra(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    value: str = Field(default="", max_length=10000)
    secret: bool = True


class ItemIn(BaseModel):
    customer_id: str
    name: str = Field(min_length=1, max_length=200)
    kind: str = "login"
    url: str | None = Field(default=None, max_length=500)
    username: str = Field(default="", max_length=255)
    password: str = Field(default="", max_length=10000)
    notes: str = Field(default="", max_length=20000)
    fields: list[Extra] = Field(default_factory=list, max_length=vault.MAX_FIELDS)
    tags: list[str] = Field(default_factory=list, max_length=20)
    node_ids: list[str] = Field(default_factory=list, max_length=200)
    ai_access: Literal["approval", "never"] = "approval"
    rotate_days: int | None = Field(default=None, ge=1, le=3650)


class ItemUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    kind: str | None = None
    url: str | None = Field(default=None, max_length=500)
    username: str | None = Field(default=None, max_length=255)
    password: str | None = Field(default=None, max_length=10000)
    notes: str | None = Field(default=None, max_length=20000)
    fields: list[Extra] | None = Field(default=None, max_length=vault.MAX_FIELDS)
    tags: list[str] | None = Field(default=None, max_length=20)
    node_ids: list[str] | None = Field(default=None, max_length=200)
    ai_access: Literal["approval", "never"] | None = None
    rotate_days: int | None = Field(default=None, ge=0, le=3650)


@router.get("/items")
async def list_items(customer_id: str | None = None, q: str | None = None, kind: str | None = None,
                     node_id: str | None = None, db: AsyncSession = Depends(get_db), _: User = Depends(current_user)):
    rows = await vault.search_items(db, customer_id=customer_id, q=q, kind=kind, node_id=node_id)
    names = await vault.machine_names(db)
    out = []
    for r in rows:
        try:
            out.append(vault.item_out(r, names=names))
        except vault.VaultError as e:
            out.append({"id": r.id, "name": r.name, "customer_id": r.customer_id, "kind": r.kind, "error": str(e)})
    return {"items": out, "kinds": list(vault.KINDS), "counts": await vault.counts(db, customer_id)}


@router.post("/items", status_code=201)
async def create_item(body: ItemIn, request: Request, db: AsyncSession = Depends(get_db),
                      sess: UserSession | None = Depends(get_session), admin: User = Depends(require_admin)):
    await _unlocked(db, sess, admin)
    try:
        item = await vault.create_item(
            db, customer_id=body.customer_id, name=body.name, kind=body.kind, url=body.url,
            payload={"username": body.username, "password": body.password, "notes": body.notes,
                     "fields": [f.model_dump() for f in body.fields]},
            tags=body.tags, node_ids=body.node_ids, ai_access=body.ai_access, rotate_days=body.rotate_days,
            by=_who(admin))
    except vault.VaultError as e:
        raise _bad(e) from e
    await vault.log_access(db, item, "create", username=admin.username, ip=client_ip(request))
    await audit(db, "vault.create", user=admin, target=item.name, ip=client_ip(request), customer=item.customer_id,
                item_id=item.id)
    await db.commit()
    events.publish("vault.changed", {"id": item.id})
    return vault.item_out(item, names=await vault.machine_names(db))


@router.get("/items/{iid}")
async def get_item(iid: str, db: AsyncSession = Depends(get_db), _: User = Depends(current_user)):
    item = await _item(db, iid)
    hist = (await db.execute(select(VaultItemHistory).where(VaultItemHistory.item_id == iid)
                             .order_by(VaultItemHistory.created_at.desc()))).scalars()
    return {**vault.item_out(item, names=await vault.machine_names(db)),
            "history": [{"id": h.id, "created_at": h.created_at, "created_by": h.created_by} for h in hist]}


@router.patch("/items/{iid}")
async def update_item(iid: str, body: ItemUpdate, request: Request, db: AsyncSession = Depends(get_db),
                      sess: UserSession | None = Depends(get_session), admin: User = Depends(require_admin)):
    await _unlocked(db, sess, admin)
    item = await _item(db, iid)
    data = body.model_dump(exclude_unset=True)
    if "fields" in data and data["fields"] is not None:
        # Secret extra fields that the editor left empty keep their stored value.
        old = {f["name"]: f for f in vault.read_secret(item)["fields"]}
        for f in data["fields"]:
            if f["secret"] and not f["value"] and f["name"] in old:
                f["value"] = old[f["name"]]["value"]
    if data.get("rotate_days") == 0:
        data["rotate_days"] = None
    try:
        changed = await vault.update_item(db, item, data, by=_who(admin))
    except vault.VaultError as e:
        raise _bad(e) from e
    if changed:
        await vault.log_access(db, item, "update", username=admin.username, ip=client_ip(request), changed=changed)
        await audit(db, "vault.update", user=admin, target=item.name, ip=client_ip(request),
                    customer=item.customer_id, item_id=item.id, changed=changed)
    await db.commit()
    events.publish("vault.changed", {"id": item.id})
    return vault.item_out(item, names=await vault.machine_names(db))


@router.delete("/items/{iid}", status_code=204)
async def delete_item(iid: str, request: Request, db: AsyncSession = Depends(get_db),
                      sess: UserSession | None = Depends(get_session), admin: User = Depends(require_admin)):
    await _unlocked(db, sess, admin)
    item = await _item(db, iid)
    await vault.log_access(db, item, "delete", username=admin.username, ip=client_ip(request))
    name, cid = item.name, item.customer_id
    await db.delete(item)
    await audit(db, "vault.delete", user=admin, target=name, ip=client_ip(request), customer=cid, item_id=iid)
    await db.commit()
    events.publish("vault.changed", {"id": iid})
    return Response(status_code=204)


class RevealIn(BaseModel):
    action: Literal["reveal", "copy"] = "reveal"
    field: str | None = Field(default=None, max_length=100)  # password, username, notes or an extra field
    reason: str | None = Field(default=None, max_length=2000)


@router.post("/items/{iid}/reveal")
async def reveal(iid: str, body: RevealIn, request: Request, db: AsyncSession = Depends(get_db),
                 sess: UserSession | None = Depends(get_session), user: User = Depends(current_user)):
    item = await _item(db, iid)
    try:
        await _unlocked(db, sess, user)
    except HTTPException:
        await vault.log_access(db, item, body.action, username=user.username, ip=client_ip(request), success=False,
                               field=body.field, error="locked or not an admin")
        await db.commit()
        raise
    try:
        out = vault.item_out(item, reveal=True, names=await vault.machine_names(db))
    except vault.VaultError as e:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e)) from e
    await vault.log_access(db, item, body.action, username=user.username, ip=client_ip(request), reason=body.reason,
                           field=body.field or "all")
    await audit(db, f"vault.{body.action}", user=user, target=item.name, ip=client_ip(request),
                customer=item.customer_id, item_id=item.id, field=body.field or "all")
    await db.commit()
    return out


@router.post("/items/{iid}/history/{hid}/reveal")
async def reveal_history(iid: str, hid: str, request: Request, db: AsyncSession = Depends(get_db),
                         sess: UserSession | None = Depends(get_session), user: User = Depends(current_user)):
    item = await _item(db, iid)
    await _unlocked(db, sess, user)
    h = await db.get(VaultItemHistory, hid)
    if h is None or h.item_id != iid:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="not_found")
    p = vault.history_secret(h)
    await vault.log_access(db, item, "reveal", username=user.username, ip=client_ip(request), field="history",
                           history_id=h.id)
    await audit(db, "vault.reveal", user=user, target=item.name, ip=client_ip(request), customer=item.customer_id,
                item_id=item.id, field="history")
    await db.commit()
    return {"id": h.id, "created_at": h.created_at, "username": p["username"], "password": p["password"]}


@router.delete("/items/{iid}/grants", status_code=204)
async def revoke_item_grants(iid: str, request: Request, db: AsyncSession = Depends(get_db),
                             admin: User = Depends(require_admin)):
    item = await _item(db, iid)
    n = await vault.revoke_grants(db, item.id)
    await vault.log_access(db, item, "revoke", username=admin.username, ip=client_ip(request), grants=n)
    await db.commit()
    return Response(status_code=204)


# ------------------------------------------------------------------ access log and AI grants
@router.get("/access")
async def access(item_id: str | None = None, customer_id: str | None = None, actor: str | None = None,
                 limit: int = 200, db: AsyncSession = Depends(get_db), _: User = Depends(require_admin)):
    rows = await vault.access_log(db, item_id=item_id, customer_id=customer_id, actor_kind=actor, limit=limit)
    return [vault.access_out(a) for a in rows]


@router.get("/grants")
async def grants(db: AsyncSession = Depends(get_db), _: User = Depends(require_admin)):
    out = []
    for g in await vault.active_grants(db):
        item = await db.get(VaultItem, g.item_id)
        out.append({"id": g.id, "item_id": g.item_id, "item_name": item.name if item else None,
                    "customer_id": g.customer_id, "token_id": g.token_id, "approval_id": g.approval_id,
                    "approved_by": g.approved_by, "created_at": g.created_at, "expires_at": g.expires_at,
                    "uses_left": g.uses_left})
    return out


@router.delete("/grants", status_code=204)
async def revoke_all_grants(request: Request, db: AsyncSession = Depends(get_db), admin: User = Depends(require_admin)):
    n = await vault.revoke_grants(db)
    await vault.log_access(db, None, "revoke", username=admin.username, ip=client_ip(request), grants=n)
    await db.commit()
    return Response(status_code=204)
