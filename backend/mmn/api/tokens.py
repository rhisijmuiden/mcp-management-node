"""Personal API tokens (used by Claude through the MCP endpoint, or by scripts)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mmn.api.deps import client_ip, current_user
from mmn.db import get_db
from mmn.models import ApiToken, Role, User
from mmn.security import hash_token, new_token
from mmn.services.audit import audit

router = APIRouter(prefix="/api/tokens", tags=["tokens"])

TOKEN_PREFIX = "mmn_"

# Scopes a token can carry. Independently a token can be tied to one or more customers
# (customer_ids): it then only sees the machines, UniFi connections, jobs and alerts of those.
SCOPES: dict[str, str] = {
    "read": "Read status, version, settings (without secrets), backups and audit log",
    "backups": "Run backups",
    "restore": "Restore a backup (destructive)",
    "updates": "Check for updates, switch channel and install updates",
    "settings": "Change mail settings and send test mail",
    "nodes": "Manage environments and nodes: pairing codes, CPU governor, notifications, agent updates",
    "node_updates": "Install package updates on nodes and reboot nodes",
    "logs": "Read logs (journal, Event Log, log files), service status, installed packages and files on nodes and servers",
    "services": "Start, stop and restart services on nodes and servers",
    "exec": "Run commands (shell or PowerShell) on nodes, servers and in containers as root/SYSTEM (every command is logged)",
    "copy": "Copy files and folders from one server to another",
    "guests": "Start, stop and restart VMs and containers, take snapshots, change CPU/memory",
    "guests_admin": "Create, delete and migrate VMs and containers, roll back snapshots, run community scripts",
    "templates": "Manage the container template library and push templates to nodes",
    "tasks": "Create, change and run scheduled tasks",
    "network": "Change bridges, firewall rules and SDN on Proxmox VE nodes (applied with backup and roll back)",
    "cluster": "Manage HA, replication, Proxmox backup jobs and storage",
    "restore_guests": "Restore VMs, containers and files from Proxmox VE backups (destructive, approval by default)",
    "deploy": "Deploy GitHub repositories into containers and change deployments",
    "unifi": "Read UniFi Network and Access: devices, clients, WiFi, firewall zones, events, doors and the access log",
    "unifi_admin": "UniFi Network actions: restart devices, power-cycle switch ports, authorize guests, switch WiFi on or off",
    "doors": "UniFi Access: unlock doors and change door lock rules (every action is logged)",
    "workstations": "Remote actions on workstations: Defender scans and signature updates, Group Policy refresh, "
                    "messages to the user and logging users off",
    "reports": "Mail monthly customer reports to the customer's contacts",
    "hub": "Use the tools of other MCP servers registered in the MCP hub (Keystone, GitHub, ...), within the guardrails",
}
ADMIN_ONLY_SCOPES = {"restore", "updates", "settings", "backups", "nodes", "node_updates", "logs", "services", "exec", "copy",
                     "guests", "guests_admin", "restore_guests", "templates", "tasks", "network", "cluster", "deploy",
                     "unifi", "unifi_admin", "doors", "hub",
                     "workstations", "reports"}


async def clean_customer_ids(db: AsyncSession, ids: list[str] | None) -> list[str] | None:
    """None = all customers. Otherwise the ids of existing customers (at least one)."""
    if ids is None:
        return None
    from mmn.models import Environment

    wanted = sorted(set(ids))
    found = set((await db.execute(select(Environment.id).where(Environment.id.in_(wanted)))).scalars()) if wanted else set()
    if not wanted or found != set(wanted):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="unknown_customer")
    return wanted


class TokenCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    scopes: list[str] = Field(default_factory=lambda: ["read"])
    expires_days: int | None = Field(default=90, ge=1, le=3650)
    customer_ids: list[str] | None = Field(default=None, max_length=500)

    @field_validator("scopes")
    @classmethod
    def _valid(cls, v: list[str]) -> list[str]:
        unknown = set(v) - set(SCOPES)
        if unknown:
            raise ValueError(f"unknown scopes: {', '.join(sorted(unknown))}")
        return sorted(set(v) | {"read"})


class TokenOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    name: str
    prefix: str
    scopes: list[str]
    created_at: datetime
    expires_at: datetime | None
    last_used_at: datetime | None
    revoked: bool
    kind: str
    last_client: dict
    customer_ids: list[str] | None = None


class TokenCreated(TokenOut):
    token: str


@router.get("/scopes")
async def list_scopes(user: User = Depends(current_user)):
    allowed = SCOPES if user.role == Role.admin else {k: v for k, v in SCOPES.items()
                                                      if k not in ADMIN_ONLY_SCOPES}
    return [{"scope": k, "description": v} for k, v in allowed.items()]


@router.get("", response_model=list[TokenOut])
async def list_tokens(db: AsyncSession = Depends(get_db), user: User = Depends(current_user)):
    rows = await db.execute(select(ApiToken).where(ApiToken.user_id == user.id, ApiToken.revoked.is_(False),
                                                   ApiToken.kind == "personal")
                            .order_by(ApiToken.created_at.desc()))
    return rows.scalars().all()


@router.get("/connections")
async def list_connections(db: AsyncSession = Depends(get_db), user: User = Depends(current_user)):
    """AI apps connected through OAuth (Claude, ChatGPT, Copilot, ...)."""
    from mmn.models import OAuthClient

    rows = (await db.execute(
        select(ApiToken, OAuthClient).join(OAuthClient, OAuthClient.client_id == ApiToken.oauth_client_id)
        .where(ApiToken.user_id == user.id, ApiToken.kind == "oauth", ApiToken.revoked.is_(False))
        .order_by(ApiToken.created_at.desc()))).all()
    out: dict[str, dict] = {}
    for tok, client in rows:
        if client.client_id in out:
            continue
        out[client.client_id] = {"client_id": client.client_id, "client_name": client.client_name,
                                 "scopes": tok.scopes, "customer_ids": tok.customer_ids, "connected_at": client.created_at,
                                 "last_used_at": tok.last_used_at, "last_client": tok.last_client or {}}
    return list(out.values())


@router.delete("/connections/{client_id}", status_code=204)
async def disconnect(client_id: str, request: Request, db: AsyncSession = Depends(get_db),
                     user: User = Depends(current_user)):
    toks = (await db.execute(select(ApiToken).where(ApiToken.user_id == user.id,
                                                    ApiToken.oauth_client_id == client_id))).scalars().all()
    for t in toks:
        t.revoked, t.refresh_hash = True, None
    await audit(db, "oauth.disconnect", user=user, target=toks[0].name if toks else client_id, ip=client_ip(request))
    await db.commit()
    return Response(status_code=204)


class CustomersIn(BaseModel):
    customer_ids: list[str] | None = Field(default=None, max_length=500)


@router.put("/connections/{client_id}/customers")
async def set_connection_customers(client_id: str, body: CustomersIn, request: Request,
                                   db: AsyncSession = Depends(get_db), user: User = Depends(current_user)):
    """Limit an AI app connected through OAuth to some customers (also for its next refresh)."""
    ids = await clean_customer_ids(db, body.customer_ids)
    toks = (await db.execute(select(ApiToken).where(ApiToken.user_id == user.id, ApiToken.oauth_client_id == client_id,
                                                    ApiToken.revoked.is_(False)))).scalars().all()
    if not toks:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    for t in toks:
        t.customer_ids = ids
    await audit(db, "oauth.customers", user=user, target=toks[0].name, ip=client_ip(request), customers=ids or "all")
    await db.commit()
    return {"customer_ids": ids}


@router.put("/{token_id}/customers", response_model=TokenOut)
async def set_token_customers(token_id: str, body: CustomersIn, request: Request, db: AsyncSession = Depends(get_db),
                              user: User = Depends(current_user)):
    tok = await db.get(ApiToken, token_id)
    if tok is None or tok.revoked or (tok.user_id != user.id and user.role != Role.admin):
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    tok.customer_ids = await clean_customer_ids(db, body.customer_ids)
    await audit(db, "token.customers", user=user, target=tok.name, ip=client_ip(request),
                customers=tok.customer_ids or "all")
    await db.commit()
    return tok


class ScopesIn(BaseModel):
    add: list[str] = Field(default_factory=list)
    remove: list[str] = Field(default_factory=list)


def _change_scopes(current: list[str], body: ScopesIn, user: User) -> list[str]:
    unknown = (set(body.add) | set(body.remove)) - set(SCOPES)
    if unknown:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="unknown_scope")
    if user.role != Role.admin and set(body.add) & ADMIN_ONLY_SCOPES:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="scope_not_allowed")
    return sorted((set(current or []) | set(body.add) | {"read"}) - (set(body.remove) - {"read"}))


@router.post("/{token_id}/scopes", response_model=TokenOut)
async def change_token_scopes(token_id: str, body: ScopesIn, request: Request, db: AsyncSession = Depends(get_db),
                              user: User = Depends(current_user)):
    """Add or remove scopes of an existing token (for example ``hub`` for the MCP hub)."""
    tok = await db.get(ApiToken, token_id)
    if tok is None or tok.revoked or tok.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    tok.scopes = _change_scopes(tok.scopes, body, user)
    await audit(db, "token.scopes", user=user, target=tok.name, ip=client_ip(request), scopes=tok.scopes)
    await db.commit()
    return tok


@router.post("/connections/{client_id}/scopes")
async def change_connection_scopes(client_id: str, body: ScopesIn, request: Request,
                                   db: AsyncSession = Depends(get_db), user: User = Depends(current_user)):
    """Add or remove scopes of an AI app connected through OAuth (also kept on its next refresh)."""
    toks = (await db.execute(select(ApiToken).where(ApiToken.user_id == user.id, ApiToken.oauth_client_id == client_id,
                                                    ApiToken.revoked.is_(False)))).scalars().all()
    if not toks:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    for t in toks:
        t.scopes = _change_scopes(t.scopes, body, user)
    await audit(db, "oauth.scopes", user=user, target=toks[0].name, ip=client_ip(request), scopes=toks[0].scopes)
    await db.commit()
    return {"scopes": toks[0].scopes}


@router.post("", response_model=TokenCreated, status_code=201)
async def create_token(body: TokenCreate, request: Request, db: AsyncSession = Depends(get_db),
                       user: User = Depends(current_user)):
    if user.role != Role.admin and set(body.scopes) & ADMIN_ONLY_SCOPES:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="scope_not_allowed")
    customer_ids = await clean_customer_ids(db, body.customer_ids)
    raw = TOKEN_PREFIX + new_token(32)
    tok = ApiToken(user_id=user.id, name=body.name, token_hash=hash_token(raw), prefix=raw[:12],
                   scopes=body.scopes, customer_ids=customer_ids,
                   expires_at=datetime.now(UTC) + timedelta(days=body.expires_days) if body.expires_days else None)
    db.add(tok)
    await db.flush()
    await audit(db, "token.create", user=user, target=body.name, ip=client_ip(request), scopes=body.scopes,
                customers=customer_ids or "all")
    await db.commit()
    return TokenCreated(**TokenOut.model_validate(tok).model_dump(), token=raw)


@router.delete("/{token_id}", status_code=204)
async def revoke_token(token_id: str, request: Request, db: AsyncSession = Depends(get_db),
                       user: User = Depends(current_user)):
    tok = await db.get(ApiToken, token_id)
    if tok is None or (tok.user_id != user.id and user.role != Role.admin):
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    tok.revoked = True
    await audit(db, "token.revoke", user=user, target=tok.name, ip=client_ip(request))
    await db.commit()
    return Response(status_code=204)


async def resolve_token(db: AsyncSession, raw: str) -> ApiToken | None:
    if not raw.startswith(TOKEN_PREFIX):
        return None
    tok = (await db.execute(select(ApiToken).where(ApiToken.token_hash == hash_token(raw)))).scalar_one_or_none()
    now = datetime.now(UTC)
    if tok is None or tok.revoked or (tok.expires_at and tok.expires_at < now) or not tok.user.is_active:
        return None
    if not tok.last_used_at or now - tok.last_used_at > timedelta(minutes=1):
        await db.execute(ApiToken.__table__.update().where(ApiToken.id == tok.id).values(last_used_at=now))
        await db.commit()
    return tok


def effective_scopes(tok: ApiToken) -> set[str]:
    """A token never has more rights than its owner has right now."""
    scopes = set(tok.scopes or [])
    if tok.user.role != Role.admin:
        scopes -= ADMIN_ONLY_SCOPES
    return scopes
