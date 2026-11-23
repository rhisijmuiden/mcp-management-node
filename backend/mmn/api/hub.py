"""MCP hub API: register other MCP servers, accept their tools, the tool catalog and hub access per AI app."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mmn.api.deps import client_ip, require_admin
from mmn.db import get_db
from mmn.models import AiPolicy, ApiToken, Environment, McpUpstream, Node, OAuthClient, User
from mmn.services import events, gateway, hub, tenancy
from mmn.services.audit import audit
from mmn.services.hub_client import HubError, forget

router = APIRouter(prefix="/api/hub", tags=["hub"])


def _err(e: Exception) -> HTTPException:
    code = getattr(e, "code", "invalid")
    return HTTPException(status.HTTP_400_BAD_REQUEST, detail={"code": code, "message": str(e)})


async def _get(db: AsyncSession, uid: str) -> McpUpstream:
    with tenancy.unrestricted():
        u = await db.get(McpUpstream, uid)
    if u is None or not tenancy.is_allowed(u.environment_id) and tenancy.allowed() is not None \
            and u.environment_id is not None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="not_found")
    return u


class UpstreamIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    namespace: str = Field(min_length=1, max_length=32)
    description: str | None = Field(default=None, max_length=2000)
    customer_id: str | None = None
    transport: str = "http"
    url: str | None = Field(default=None, max_length=2000)
    via_agent: bool = False
    agent_node_id: str | None = None
    tls_mode: str = "verify"
    fingerprint: str | None = None
    command: str | None = Field(default=None, max_length=2000)
    args: list[str] = Field(default_factory=list, max_length=50)
    auth_kind: str = "none"
    # Secrets: None = keep what is stored. An empty value removes it.
    token: str | None = Field(default=None, max_length=8000)
    secret_headers: dict[str, str] | None = None
    secret_env: dict[str, str] | None = None
    headers_public: dict[str, str] = Field(default_factory=dict)
    env_public: dict[str, str] = Field(default_factory=dict)
    token_env: str | None = Field(default=None, max_length=100)
    oauth_client_id: str | None = Field(default=None, max_length=500)
    oauth_client_secret: str | None = Field(default=None, max_length=2000)
    oauth_scopes: str | None = Field(default=None, max_length=1000)
    oauth_authorize_url: str | None = Field(default=None, max_length=2000)
    oauth_token_url: str | None = Field(default=None, max_length=2000)
    timeout: int = Field(default=60, ge=5, le=600)
    enabled: bool = True
    preset: str | None = None


def _clean_map(m: dict[str, str] | None, kind: str) -> dict[str, str] | None:
    if m is None:
        return None
    out = {}
    for k, v in m.items():
        k = (k or "").strip()
        if not k:
            continue
        if len(k) > 100 or any(c in k for c in "\r\n:= ") and kind == "header":
            raise hub.HubConfigError(f"invalid header name {k!r}", "bad_header")
        if kind == "env" and not k.replace("_", "").isalnum():
            raise hub.HubConfigError(f"invalid variable name {k!r}", "bad_env")
        out[k] = str(v)
    return out


async def _apply(db: AsyncSession, u: McpUpstream, body: UpstreamIn, *, new: bool) -> None:
    if body.transport not in hub.TRANSPORTS:
        raise hub.HubConfigError("unknown transport", "bad_transport")
    if body.auth_kind not in hub.AUTH_KINDS:
        raise hub.HubConfigError("unknown sign-in method", "bad_auth")
    if body.tls_mode not in hub.TLS_MODES:
        raise hub.HubConfigError("unknown TLS mode", "bad_tls")
    ns = hub.validate_namespace(body.namespace)
    with tenancy.unrestricted():
        clash = (await db.execute(select(McpUpstream).where(McpUpstream.namespace == ns,
                                                            McpUpstream.id != (u.id or "")))).scalars().first()
    if clash is not None:
        raise hub.HubConfigError("another MCP server already uses this prefix", "namespace_taken")
    if not new and ns != u.namespace and any(t for t in (u.tools or [])):
        # Tool names change with the prefix: they have to be accepted again.
        u.tool_settings = {}
    if body.customer_id:
        if await db.get(Environment, body.customer_id) is None:
            raise hub.HubConfigError("unknown customer", "unknown_customer")
    elif tenancy.allowed() is not None:
        raise hub.HubConfigError("pick the customer, or switch the customer switcher to all customers",
                                 "customer_required")
    if body.transport == "stdio":
        if not (body.command or "").strip():
            raise hub.HubConfigError("enter the command that starts the MCP server", "command_required")
        if body.via_agent:
            raise hub.HubConfigError("stdio servers run on this server; use Streamable HTTP through an agent",
                                     "bad_transport")
    else:
        url = (body.url or "").strip()
        if not url.startswith(("https://", "http://")):
            raise hub.HubConfigError("enter an http(s):// address", "bad_url")
    agent_id = None
    if body.via_agent:
        if body.transport != "http":
            raise hub.HubConfigError("only Streamable HTTP servers can be reached through an agent", "bad_transport")
        n = await db.get(Node, body.agent_node_id or "")
        if n is None:
            raise hub.HubConfigError("choose the agent that reaches this MCP server", "no_agent")
        if body.customer_id and n.environment_id != body.customer_id:
            raise hub.HubConfigError("the agent must belong to the same customer", "agent_customer")
        agent_id = n.id
    fp = None
    if body.tls_mode == "pin":
        from mmn.services.unifi.transport import UnifiError, normalize_fingerprint

        try:
            fp = normalize_fingerprint(body.fingerprint)
        except UnifiError as e:
            raise hub.HubConfigError(str(e), "bad_fingerprint") from e
        if not fp:
            raise hub.HubConfigError("pinning needs a certificate fingerprint", "bad_fingerprint")
    u.name, u.namespace, u.description = body.name.strip(), ns, (body.description or "").strip() or None
    u.environment_id = body.customer_id or None
    u.transport = body.transport
    u.url = (body.url or "").strip() or None if body.transport != "stdio" else None
    u.command = (body.command or "").strip() or None if body.transport == "stdio" else None
    u.args = [a for a in body.args if a] if body.transport == "stdio" else []
    u.agent_node_id, u.tls_mode, u.fingerprint = agent_id, body.tls_mode, fp
    u.auth_kind, u.enabled = body.auth_kind, body.enabled
    cfg = dict(u.config or {})
    cfg.update(timeout=body.timeout, headers_public=_clean_map(body.headers_public, "header") or {},
               env_public=_clean_map(body.env_public, "env") or {},
               token_env=(body.token_env or "").strip() or None, preset=body.preset or cfg.get("preset"),
               oauth_scopes=(body.oauth_scopes or "").strip() or None,
               oauth_authorize_url=(body.oauth_authorize_url or "").strip() or None,
               oauth_token_url=(body.oauth_token_url or "").strip() or None)
    sec = hub.secrets_of(u)
    if body.oauth_client_id is not None:
        newid = body.oauth_client_id.strip() or None
        if newid != cfg.get("oauth_client_id"):
            sec.pop("oauth", None)
            cfg.pop("oauth_registered", None)
        cfg["oauth_client_id"] = newid
    if body.token is not None:
        sec["token"] = body.token.strip()
    if body.secret_headers is not None:
        old = sec.get("headers") or {}
        sec["headers"] = {k: (v if v != "***" else old.get(k, "")) for k, v in
                          (_clean_map(body.secret_headers, "header") or {}).items()}
    if body.secret_env is not None:
        old = sec.get("env") or {}
        sec["env"] = {k: (v if v != "***" else old.get(k, "")) for k, v in
                      (_clean_map(body.secret_env, "env") or {}).items()}
    if body.oauth_client_secret is not None:
        sec["oauth_client_secret"] = body.oauth_client_secret.strip()
    if body.auth_kind != "oauth":
        sec.pop("oauth", None)
    u.config = cfg
    hub.set_secrets(u, sec)
    if not u.enabled:
        u.status = "disabled"
    elif u.status == "disabled":
        u.status = "unknown"
    forget(u.id)


async def _agents(db: AsyncSession) -> list[dict[str, Any]]:
    from mmn.services import agent_hub

    rows = (await db.execute(select(Node).order_by(Node.name))).scalars()
    return [{"id": n.id, "name": n.name, "customer_id": n.environment_id, "online": agent_hub.is_online(n.id)}
            for n in rows]


# ------------------------------------------------------------------ upstreams
@router.get("/upstreams")
async def list_upstreams(db: AsyncSession = Depends(get_db), _: User = Depends(require_admin)):
    names = await hub.env_names(db)
    allowed = tenancy.allowed()
    with tenancy.unrestricted():
        rows = list((await db.execute(select(McpUpstream).order_by(McpUpstream.name))).scalars())
    out = [hub.upstream_out(u, names) for u in rows
           if allowed is None or u.environment_id is None or u.environment_id in allowed]
    return {"upstreams": out, "presets": hub.PRESETS, "agents": await _agents(db),
            "customers": [{"id": k, "name": v} for k, v in sorted(names.items(), key=lambda kv: kv[1].lower())]}


@router.post("/upstreams", status_code=201)
async def create_upstream(body: UpstreamIn, request: Request, db: AsyncSession = Depends(get_db),
                          admin: User = Depends(require_admin)):
    u = McpUpstream(created_by=admin.username, tools=[], tool_settings={}, config={}, args=[])
    try:
        await _apply(db, u, body, new=True)
    except hub.HubConfigError as e:
        raise _err(e) from e
    db.add(u)
    await db.flush()
    await audit(db, "hub.create", user=admin, target=u.name, ip=client_ip(request), namespace=u.namespace,
                transport=u.transport, url=u.url, via_agent=bool(u.agent_node_id), customer=u.environment_id)
    await db.commit()
    # First contact right away, so the admin sees the tools to accept.
    if u.enabled:
        try:
            await hub.refresh(db, u)
        except HubError:
            pass
        await db.commit()
    await hub.sync(db)
    return hub.upstream_out(u, await hub.env_names(db), full=True)


@router.get("/upstreams/{uid}")
async def get_upstream(uid: str, db: AsyncSession = Depends(get_db), _: User = Depends(require_admin)):
    u = await _get(db, uid)
    return hub.upstream_out(u, await hub.env_names(db), full=True)


@router.put("/upstreams/{uid}")
async def update_upstream(uid: str, body: UpstreamIn, request: Request, db: AsyncSession = Depends(get_db),
                          admin: User = Depends(require_admin)):
    u = await _get(db, uid)
    try:
        await _apply(db, u, body, new=False)
    except hub.HubConfigError as e:
        raise _err(e) from e
    await audit(db, "hub.update", user=admin, target=u.name, ip=client_ip(request), namespace=u.namespace,
                transport=u.transport, url=u.url, enabled=u.enabled)
    await db.commit()
    if u.enabled:
        try:
            await hub.refresh(db, u)
        except HubError:
            pass
        await db.commit()
    await hub.sync(db)
    events.publish("hub.changed", {"id": u.id})
    return hub.upstream_out(u, await hub.env_names(db), full=True)


@router.delete("/upstreams/{uid}", status_code=204)
async def delete_upstream(uid: str, request: Request, db: AsyncSession = Depends(get_db),
                          admin: User = Depends(require_admin)):
    u = await _get(db, uid)
    forget(u.id)
    await db.delete(u)
    await audit(db, "hub.delete", user=admin, target=u.name, ip=client_ip(request), namespace=u.namespace)
    await db.commit()
    await hub.sync(db)
    events.publish("hub.changed", {"id": uid})
    return Response(status_code=204)


@router.post("/upstreams/{uid}/refresh")
async def refresh_upstream(uid: str, request: Request, db: AsyncSession = Depends(get_db),
                           admin: User = Depends(require_admin)):
    u = await _get(db, uid)
    try:
        diff = await hub.refresh(db, u)
    except HubError as e:
        await db.commit()
        raise _err(e) from e
    await db.commit()
    await hub.sync(db)
    return {"diff": diff, "upstream": hub.upstream_out(u, await hub.env_names(db), full=True)}


class AcceptIn(BaseModel):
    names: list[str] | None = None  # None = every new or changed tool


@router.post("/upstreams/{uid}/accept")
async def accept(uid: str, body: AcceptIn, request: Request, db: AsyncSession = Depends(get_db),
                 admin: User = Depends(require_admin)):
    u = await _get(db, uid)
    done = hub.accept_tools(u, body.names)
    await audit(db, "hub.tools.accept", user=admin, target=u.name, ip=client_ip(request), tools=done)
    await db.commit()
    await hub.sync(db)
    events.publish("hub.changed", {"id": u.id})
    return hub.upstream_out(u, await hub.env_names(db), full=True)


class ToolIn(BaseModel):
    enabled: bool | None = None
    risk: str | None = None  # "auto" = the suggested risk


@router.put("/upstreams/{uid}/tools/{name}")
async def set_tool(uid: str, name: str, body: ToolIn, request: Request, db: AsyncSession = Depends(get_db),
                   admin: User = Depends(require_admin)):
    u = await _get(db, uid)
    try:
        hub.set_tool(u, name, enabled=body.enabled, risk=None if body.risk == "auto" else body.risk,
                     clear_risk=body.risk == "auto")
    except hub.HubConfigError as e:
        raise _err(e) from e
    await audit(db, "hub.tool", user=admin, target=f"{u.namespace}__{name}"[:255], ip=client_ip(request),
                enabled=body.enabled, risk=body.risk)
    await db.commit()
    await hub.sync(db)
    return hub.upstream_out(u, await hub.env_names(db), full=True)


class BulkRiskIn(BaseModel):
    names: list[str]
    risk: str


@router.post("/upstreams/{uid}/tools/risk")
async def bulk_risk(uid: str, body: BulkRiskIn, request: Request, db: AsyncSession = Depends(get_db),
                    admin: User = Depends(require_admin)):
    u = await _get(db, uid)
    try:
        for n in body.names:
            hub.set_tool(u, n, risk=None if body.risk == "auto" else body.risk, clear_risk=body.risk == "auto")
    except hub.HubConfigError as e:
        raise _err(e) from e
    await audit(db, "hub.tool.risk", user=admin, target=u.name, ip=client_ip(request), tools=body.names,
                risk=body.risk)
    await db.commit()
    await hub.sync(db)
    return hub.upstream_out(u, await hub.env_names(db), full=True)


class CertIn(BaseModel):
    url: str
    agent_node_id: str | None = None


@router.post("/certificate")
async def certificate(body: CertIn, db: AsyncSession = Depends(get_db), _: User = Depends(require_admin)):
    from mmn.services.unifi.transport import UnifiError, fetch_certificate

    try:
        return await fetch_certificate(body.url, agent_node_id=body.agent_node_id or None)
    except UnifiError as e:
        raise _err(e) from e


# ------------------------------------------------------------------ OAuth sign-in to an upstream
async def _redirect_uri(db: AsyncSession, request: Request) -> str:
    from mmn.services.links import public_base_url

    return f"{await public_base_url(db, request)}/api/hub/oauth/callback"


@router.post("/upstreams/{uid}/oauth/start")
async def oauth_start(uid: str, request: Request, db: AsyncSession = Depends(get_db),
                      admin: User = Depends(require_admin)):
    u = await _get(db, uid)
    try:
        url = await hub.oauth_start(db, u, await _redirect_uri(db, request))
    except (hub.HubConfigError, HubError) as e:
        raise _err(e) from e
    await db.commit()
    return {"authorize_url": url, "redirect_uri": await _redirect_uri(db, request)}


@router.get("/oauth/callback", include_in_schema=False)
async def oauth_callback(request: Request, state: str = "", code: str = "", error: str = "",
                         db: AsyncSession = Depends(get_db), admin: User = Depends(require_admin)):
    from mmn.services.links import public_base_url

    base = await public_base_url(db, request)
    if error or not code:
        return RedirectResponse(f"{base}/ai/hub?oauth=error&message={quote(error or 'no code')}", status_code=303)
    try:
        u = await hub.oauth_finish(db, state, code)
    except (hub.HubConfigError, HubError) as e:
        await db.rollback()
        return RedirectResponse(f"{base}/ai/hub?oauth=error&message={quote(str(e)[:300])}", status_code=303)
    await audit(db, "hub.oauth.connect", user=admin, target=u.name, ip=client_ip(request))
    await db.commit()
    try:
        await hub.refresh(db, u)
    except HubError:
        pass
    await db.commit()
    await hub.sync(db)
    return RedirectResponse(f"{base}/ai/hub?oauth=ok&id={u.id}", status_code=303)


@router.post("/upstreams/{uid}/oauth/disconnect")
async def oauth_disconnect(uid: str, request: Request, db: AsyncSession = Depends(get_db),
                           admin: User = Depends(require_admin)):
    u = await _get(db, uid)
    sec = hub.secrets_of(u)
    sec.pop("oauth", None)
    hub.set_secrets(u, sec)
    u.status = "auth_required"
    forget(u.id)
    await audit(db, "hub.oauth.disconnect", user=admin, target=u.name, ip=client_ip(request))
    await db.commit()
    return hub.upstream_out(u, await hub.env_names(db), full=True)


# ------------------------------------------------------------------ tool catalog
@router.get("/catalog")
async def catalog(token_id: str | None = None, db: AsyncSession = Depends(get_db), _: User = Depends(require_admin)):
    """Every tool the AI can get (built in and from the MCP hub) with its risk level, and whether the
    defaults, or one token, switched it off (a "block" rule)."""
    await hub.sync(db)
    g = await gateway.get_policy(db, "global")
    t = await gateway.get_policy(db, "token", token_id) if token_id else None
    g_rules, t_rules = dict(g.tools or {}) if g else {}, dict(t.tools or {}) if t else {}
    names = await hub.env_names(db)
    with tenancy.unrestricted():
        ups = {u.id: u for u in (await db.execute(select(McpUpstream))).scalars()}
    tools = []
    for c in gateway.catalog():
        ns = gateway.namespace_of(c["name"]) if c["source"] == "hub" else None
        g_rule = g_rules.get(c["name"]) or (g_rules.get(f"{ns}__*") if ns else None)
        t_rule = t_rules.get(c["name"]) or (t_rules.get(f"{ns}__*") if ns else None)
        u = ups.get(c["upstream_id"] or "")
        tools.append({**c, "upstream_name": u.name if u else None, "namespace": ns,
                      "customer_name": names.get(c["customer_id"] or ""), "default_rule": g_rule,
                      "token_rule": t_rule, "enabled": g_rule != "block" and t_rule != "block"})
    groups = [{"id": "stackpilot", "name": "Stackpilot", "namespace": None, "default_rule": None, "token_rule": None}]
    for u in sorted(ups.values(), key=lambda u: u.name.lower()):
        groups.append({"id": u.id, "name": u.name, "namespace": u.namespace, "status": u.status, "enabled": u.enabled,
                       "default_rule": g_rules.get(f"{u.namespace}__*"), "token_rule": t_rules.get(f"{u.namespace}__*"),
                       "pending_count": len(hub.pending_tools(u))})
    toks = list((await db.execute(select(ApiToken).where(ApiToken.revoked.is_(False))
                                  .order_by(ApiToken.created_at.desc()))).scalars())
    return {"tools": tools, "groups": groups,
            "tokens": [{"token_id": x.id, "name": x.name, "kind": x.kind, "username": x.user.username,
                        "hub": "hub" in (x.scopes or [])} for x in toks]}


class SwitchIn(BaseModel):
    key: str  # a tool name or "<prefix>__*"
    enabled: bool
    token_id: str | None = None  # None = the defaults (every token)


@router.post("/catalog/switch")
async def switch(body: SwitchIn, request: Request, db: AsyncSession = Depends(get_db),
                 admin: User = Depends(require_admin)):
    """Switch a tool (or a whole MCP hub server) on or off, for every token or for one token. Stored as a
    "block" rule in the policies, so it also shows on Policies & guardrails."""
    if not gateway.valid_rule_key(body.key):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="unknown_tool")
    scope, sid = ("token", body.token_id) if body.token_id else ("global", None)
    if body.token_id and await db.get(ApiToken, body.token_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="not_found")
    p = await gateway.get_policy(db, scope, sid)
    risks = dict(p.risks or {}) if p else {}
    tools = dict(p.tools or {}) if p else {}
    if body.enabled:
        if tools.get(body.key) == "block":
            tools.pop(body.key)
    else:
        tools[body.key] = "block"
    ow = p.outside_window if p else ("approve" if scope == "global" else "inherit")
    saved = await gateway.save_policy(db, scope, sid, risks=risks, tools=tools, outside_window=ow,
                                      paused=bool(p.paused) if p else False, by=admin.username)
    await audit(db, f"ai.catalog.{'on' if body.enabled else 'off'}", user=admin, target=body.key[:255],
                ip=client_ip(request), token=body.token_id or "all")
    await db.commit()
    events.publish("gateway.policy", {"scope": scope})
    return gateway.policy_out(saved, scope, sid)


# ------------------------------------------------------------------ which AI apps may use the hub
@router.get("/access")
async def access(db: AsyncSession = Depends(get_db), admin: User = Depends(require_admin)):
    toks = list((await db.execute(select(ApiToken).where(ApiToken.user_id == admin.id, ApiToken.revoked.is_(False))
                                  .order_by(ApiToken.created_at.desc()))).scalars())
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for t in toks:
        if t.kind == "oauth":
            if not t.oauth_client_id or t.oauth_client_id in seen:
                continue
            seen.add(t.oauth_client_id)
            c = await db.get(OAuthClient, t.oauth_client_id)
            out.append({"kind": "oauth", "id": t.oauth_client_id, "name": c.client_name if c else t.name,
                        "hub": "hub" in (t.scopes or []), "customer_ids": t.customer_ids,
                        "last_used_at": t.last_used_at})
        else:
            out.append({"kind": "personal", "id": t.id, "name": t.name, "hub": "hub" in (t.scopes or []),
                        "customer_ids": t.customer_ids, "last_used_at": t.last_used_at})
    pol = list((await db.execute(select(AiPolicy).where(AiPolicy.scope == "token"))).scalars())
    return {"apps": out, "token_policies": len(pol)}
