"""MCP hub: other MCP servers ("upstreams") whose tools Stackpilot offers through its own MCP endpoint.

* Every upstream gets a namespace; its tools appear as ``<namespace>__<tool>`` (Claude and most clients
  only accept letters, digits, ``_`` and ``-`` in tool names, so no dot).
* Tools are only offered after an admin accepted them. The accepted version (name, description, input
  schema and annotations) is stored as a hash; a tool that is new or changed later waits for a new
  accept, so a server can't slip new instructions to the AI ("tool poisoning").
* Each tool has a risk level, suggested from its MCP annotations and adjustable, and goes through the same
  scopes (``hub``), guardrails, approvals and audit log as the built-in tools.
* An upstream can belong to a customer: only tokens for that customer (or for all customers) see it, and
  its calls count for that customer's policies and maintenance window. A shared upstream (GitHub) is
  only for tokens for all customers.
* Secrets (bearer token, headers, environment variables, OAuth tokens) are stored encrypted.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
import secrets
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode, urlsplit

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mmn.config import get_settings
from mmn.db import sessionmaker
from mmn.models import Environment, McpUpstream
from mmn.security import decrypt_secret, encrypt_secret
from mmn.services import events, gateway, tenancy
from mmn.services.hub_client import Endpoint, HubError, RemoteToolError, client_for, forget

log = logging.getLogger("mmn.hub")

SEP = "__"
NAMESPACE_RE = re.compile(r"^[a-z][a-z0-9_]{0,23}$")
TRANSPORTS = ("http", "sse", "stdio")
AUTH_KINDS = ("none", "bearer", "headers", "oauth")
TLS_MODES = ("verify", "pin", "insecure")
_DESTRUCTIVE_WORDS = re.compile(r"(^|_)(delete|remove|destroy|drop|purge|wipe|erase|reset|revoke|force|kill|"
                                r"terminate|uninstall|truncate)(_|$)")


class HubConfigError(Exception):
    def __init__(self, message: str, code: str = "invalid"):
        super().__init__(message)
        self.code = code


# ------------------------------------------------------------------ presets
PRESETS: list[dict[str, Any]] = [
    {
        "id": "github", "name": "GitHub", "namespace": "github", "transport": "http",
        "url": "https://api.githubcopilot.com/mcp/", "auth_kind": "bearer",
        "description": "The official GitHub MCP server: repositories, issues, pull requests, releases and Actions.",
        "headers_hint": {"X-MCP-Toolsets": "repos,issues,pull_requests,actions,context"},
        "docs": "https://github.com/github/github-mcp-server",
        "token_hint": "Fine-grained personal access token (github.com → Settings → Developer settings). "
                      "Choose the repositories (or all), and only these permissions: Administration "
                      "(read and write, to create repositories), Contents, Issues, Pull requests, Actions and "
                      "Workflows (read and write), Metadata (read). Or a GitHub App with the same permissions.",
        "stdio": {"command": "github-mcp-server stdio", "token_env": "GITHUB_PERSONAL_ACCESS_TOKEN",
                  "env_hint": {"GITHUB_TOOLSETS": "repos,issues,pull_requests,actions,context"}},
    },
    {
        "id": "keystone", "name": "Keystone VoIP", "namespace": "keystone", "transport": "http",
        "url": "", "auth_kind": "bearer", "via_agent_recommended": True,
        "description": "The Keystone VoIP central: calls, extensions, queues and call logs.",
        "token_hint": "An API token of the Keystone MCP server. Keystone runs on the LAN: choose "
                      "\"through an agent\" with an agent in the same network, then no port has to be opened.",
    },
    {
        "id": "custom", "name": "", "namespace": "", "transport": "http", "url": "", "auth_kind": "none",
        "description": "",
    },
]


# ------------------------------------------------------------------ secrets
def secrets_of(u: McpUpstream) -> dict[str, Any]:
    try:
        return json.loads(decrypt_secret(u.secret_enc) or "{}") or {}
    except ValueError:
        return {}


def set_secrets(u: McpUpstream, data: dict[str, Any]) -> None:
    clean = {k: v for k, v in data.items() if v not in (None, "", {}, [])}
    u.secret_enc = encrypt_secret(json.dumps(clean)) if clean else None


def validate_namespace(ns: str) -> str:
    ns = (ns or "").strip().lower()
    if not NAMESPACE_RE.match(ns) or SEP in ns or ns.endswith("_"):
        raise HubConfigError("the prefix must be 1-24 lower case letters, digits or single underscores, "
                             "starting with a letter", "bad_namespace")
    from mmn.services.gateway import REGISTRY

    if any(name == ns or name.startswith(ns + SEP) for name, s in REGISTRY.items() if s.source is None):
        raise HubConfigError("this prefix clashes with a built-in tool", "bad_namespace")
    return ns


def exposed_name(namespace: str, tool: str) -> str:
    clean = re.sub(r"[^A-Za-z0-9_-]", "_", tool)
    name = f"{namespace}{SEP}{clean}"
    if len(name) > 64:
        name = name[:55] + "_" + hashlib.sha1(tool.encode()).hexdigest()[:8]
    return name


def tool_hash(t: dict[str, Any]) -> str:
    raw = json.dumps({"n": t.get("name"), "d": t.get("description") or "", "t": t.get("title") or "",
                      "s": t.get("input_schema") or {}, "a": t.get("annotations") or {}}, sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()


def suggest_risk(t: dict[str, Any]) -> str:
    a = t.get("annotations") or {}
    if a.get("readOnlyHint") is True:
        return "read"
    if a.get("destructiveHint") is True:
        return "destructive"
    if a.get("destructiveHint") is False:
        return "change"
    return "destructive" if _DESTRUCTIVE_WORDS.search(str(t.get("name") or "").lower()) else "change"


def _normalize_tool(namespace: str, raw: dict[str, Any]) -> dict[str, Any]:
    t = {"name": str(raw.get("name") or "")[:200], "title": raw.get("title") or None,
         "description": str(raw.get("description") or "")[:8000],
         "input_schema": raw.get("inputSchema") or raw.get("input_schema") or {"type": "object", "properties": {}},
         "annotations": {k: v for k, v in (raw.get("annotations") or {}).items()
                         if k in ("title", "readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint")}}
    t["exposed"] = exposed_name(namespace, t["name"])
    t["hash"] = tool_hash(t)
    t["suggested_risk"] = suggest_risk(t)
    return t


# ------------------------------------------------------------------ endpoint (connection settings + secrets)
async def endpoint(db: AsyncSession, u: McpUpstream) -> Endpoint:
    sec = secrets_of(u)
    cfg = u.config or {}
    headers: dict[str, str] = {}
    env: dict[str, str] = {k: str(v) for k, v in (cfg.get("env_public") or {}).items()}
    env.update({k: str(v) for k, v in (sec.get("env") or {}).items()})
    headers.update({k: str(v) for k, v in (cfg.get("headers_public") or {}).items()})
    token = None
    if u.auth_kind in ("bearer", "headers"):
        headers.update({k: str(v) for k, v in (sec.get("headers") or {}).items()})
        token = sec.get("token") if u.auth_kind == "bearer" else None
    elif u.auth_kind == "oauth":
        token = await oauth_access_token(db, u)
    if token:
        if u.transport == "stdio" and cfg.get("token_env"):
            env[str(cfg["token_env"])] = token
        else:
            headers["Authorization"] = f"Bearer {token}"
    if u.transport == "stdio":
        home = get_settings().data_dir / "hub"
        try:
            home.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        env.setdefault("HOME", str(home))
    return Endpoint(key=u.id, transport=u.transport, url=u.url, headers=headers, agent_node_id=u.agent_node_id,
                    tls_mode=u.tls_mode or "verify", fingerprint=u.fingerprint, command=u.command,
                    args=[str(a) for a in (u.args or [])], env=env,
                    timeout=float(cfg.get("timeout") or 60), label=u.name)


# ------------------------------------------------------------------ refresh (connect, list tools)
def _status_for(e: Exception) -> str:
    if isinstance(e, HubError) and e.code in ("unauthorized", "forbidden", "oauth_required"):
        return "auth_required"
    return "error"


async def refresh(db: AsyncSession, u: McpUpstream) -> dict[str, Any]:
    """Connect, read the server info and its tools, store them. Returns what changed."""
    started = time.monotonic()
    before = {t["name"]: t for t in (u.tools or [])}
    try:
        with tenancy.unrestricted():
            ep = await endpoint(db, u)
            c = client_for(ep)
            info = await c.info()
            raw = await c.list_tools()
    except Exception as e:  # noqa: BLE001 - stored as the upstream's state
        u.status, u.last_error = _status_for(e), (str(e) or type(e).__name__)[:2000]
        u.last_checked_at = datetime.now(UTC)
        await db.flush()
        events.publish("hub.changed", {"id": u.id})
        raise HubError(u.last_error, getattr(e, "code", "error")) from e
    tools = []
    seen = set()
    for r in raw:
        t = _normalize_tool(u.namespace, r)
        if not t["name"] or t["exposed"] in seen:
            continue
        seen.add(t["exposed"])
        t["first_seen"] = (before.get(t["name"]) or {}).get("first_seen") or datetime.now(UTC).isoformat()
        tools.append(t)
    u.tools = sorted(tools, key=lambda t: t["name"])
    u.server_info = {k: v for k, v in (info.get("server_info") or {}).items() if k in ("name", "title", "version")}
    u.server_info["protocol"] = info.get("protocol")
    instr = info.get("instructions")
    u.instructions = str(instr)[:8000] if instr else None
    u.status, u.last_error = ("online" if u.enabled else "disabled"), None
    u.last_checked_at = datetime.now(UTC)
    u.latency_ms = int((time.monotonic() - started) * 1000)
    now_names = {t["name"]: t for t in tools}
    diff = {"added": sorted(n for n in now_names if n not in before),
            "changed": sorted(n for n, t in now_names.items() if n in before and before[n].get("hash") != t["hash"]),
            "removed": sorted(n for n in before if n not in now_names)}
    diff["needs_review"] = len(pending_tools(u))
    await db.flush()
    events.publish("hub.changed", {"id": u.id})
    return diff


async def check(db: AsyncSession, u: McpUpstream) -> None:
    """Health check (from the scheduler): never raises."""
    try:
        diff = await refresh(db, u)
    except HubError:
        return
    if diff["added"] or diff["changed"]:
        from mmn.services.audit import audit

        await audit(db, "hub.tools.changed", source="system", target=u.name, added=diff["added"],
                    changed=diff["changed"], removed=diff["removed"])


# ------------------------------------------------------------------ tools: accepted, active, risk
def tool_state(u: McpUpstream, t: dict[str, Any]) -> str:
    """new | changed | active | disabled"""
    s = (u.tool_settings or {}).get(t["name"]) or {}
    if not s.get("approved_hash"):
        return "new"
    if s["approved_hash"] != t.get("hash"):
        return "changed"
    return "disabled" if s.get("enabled") is False else "active"


def pending_tools(u: McpUpstream) -> list[dict[str, Any]]:
    return [t for t in (u.tools or []) if tool_state(u, t) in ("new", "changed")]


def tool_risk(u: McpUpstream, t: dict[str, Any]) -> str:
    r = ((u.tool_settings or {}).get(t["name"]) or {}).get("risk")
    return r if r in gateway.RISKS else t.get("suggested_risk") or suggest_risk(t)


def accept_tools(u: McpUpstream, names: list[str] | None) -> list[str]:
    """Accept the current version of these tools (None = every new or changed tool)."""
    settings = {k: dict(v) for k, v in (u.tool_settings or {}).items()}
    done = []
    for t in u.tools or []:
        if (names is None and tool_state(u, t) in ("new", "changed")) or (names is not None and t["name"] in names):
            s = settings.setdefault(t["name"], {})
            s["approved_hash"] = t["hash"]
            s["approved_at"] = datetime.now(UTC).isoformat()
            s.setdefault("enabled", True)
            done.append(t["name"])
    u.tool_settings = settings
    return done


def set_tool(u: McpUpstream, name: str, *, enabled: bool | None = None, risk: str | None = None,
             clear_risk: bool = False) -> None:
    if not any(t["name"] == name for t in u.tools or []):
        raise HubConfigError("unknown tool", "not_found")
    settings = {k: dict(v) for k, v in (u.tool_settings or {}).items()}
    s = settings.setdefault(name, {})
    if enabled is not None:
        s["enabled"] = bool(enabled)
    if clear_risk:
        s.pop("risk", None)
    elif risk is not None:
        if risk not in gateway.RISKS:
            raise HubConfigError("unknown risk level", "bad_risk")
        s["risk"] = risk
    u.tool_settings = settings


# ------------------------------------------------------------------ the registry of offered tools
@dataclass
class HubTool:
    exposed: str
    upstream_id: str
    upstream_name: str
    namespace: str
    name: str  # the upstream's own name
    customer_id: str | None
    risk: str
    title: str | None
    description: str
    input_schema: dict[str, Any]
    annotations: dict[str, Any]


HUB_TOOLS: dict[str, HubTool] = {}
_synced_at = 0.0


def _caller(exposed: str):
    """The audited, guarded call (used when an approved call runs)."""

    async def call(**arguments: Any) -> Any:
        from mmn.mcp_server import run_hub_tool

        return await run_hub_tool(exposed, arguments)

    call.__name__ = exposed
    return call


def rebuild(rows: list[McpUpstream]) -> None:
    """Replace the hub tools in the gateway registry with the active tools of these upstreams."""
    global _synced_at
    new: dict[str, HubTool] = {}
    for u in rows:
        if not u.enabled:
            continue
        for t in u.tools or []:
            if tool_state(u, t) != "active":
                continue
            new[t["exposed"]] = HubTool(
                exposed=t["exposed"], upstream_id=u.id, upstream_name=u.name, namespace=u.namespace, name=t["name"],
                customer_id=u.environment_id, risk=tool_risk(u, t), title=t.get("title"),
                description=t.get("description") or "", input_schema=t.get("input_schema") or {},
                annotations=t.get("annotations") or {})
    for name in [n for n, s in gateway.REGISTRY.items() if s.source == "hub"]:
        gateway.REGISTRY.pop(name, None)
    for h in new.values():
        gateway.register(gateway.ToolSpec(
            name=h.exposed, scope="hub", risk=h.risk, all_customers=h.customer_id is None,
            description=f"[{h.upstream_name}] " + " ".join(h.description.split("\n\n")[0].split()),
            call=_caller(h.exposed), source="hub", customer_id=h.customer_id, upstream_id=h.upstream_id))
    HUB_TOOLS.clear()
    HUB_TOOLS.update(new)
    _synced_at = time.monotonic()


async def sync(db: AsyncSession | None = None) -> None:
    async def run(s: AsyncSession) -> None:
        with tenancy.unrestricted():
            rows = list((await s.execute(select(McpUpstream))).scalars())
        rebuild(rows)

    if db is not None:
        await run(db)
        return
    async with sessionmaker()() as s:
        await run(s)


async def ensure_synced(max_age: float = 30) -> None:
    if time.monotonic() - _synced_at > max_age:
        try:
            await sync()
        except Exception:  # noqa: BLE001 - keep the last known tools
            log.exception("hub sync failed")


# ------------------------------------------------------------------ calling a tool
def _flatten_result(res: dict[str, Any]) -> Any:
    """What the AI gets back: the upstream's content as it is (text, images), with an error flag."""
    return res


async def call_tool(exposed: str, arguments: dict[str, Any]) -> Any:
    h = HUB_TOOLS.get(exposed)
    if h is None:
        await sync()
        h = HUB_TOOLS.get(exposed)
    if h is None:
        raise HubError("this tool is no longer offered by the MCP hub", "not_found")
    async with sessionmaker()() as db:
        with tenancy.unrestricted():
            u = await db.get(McpUpstream, h.upstream_id)
            if u is None or not u.enabled:
                raise HubError("this MCP server is switched off in the MCP hub", "disabled")
            t = next((t for t in u.tools or [] if t["name"] == h.name), None)
            if t is None or tool_state(u, t) != "active":
                raise HubError("this tool waits for an admin to accept it in the MCP hub", "not_accepted")
            ep = await endpoint(db, u)
        await db.commit()
    c = client_for(ep)
    try:
        res = await c.call_tool(h.name, arguments)
    except HubError as e:
        if e.code == "unauthorized" and ep.headers.get("Authorization"):
            await _mark(h.upstream_id, "auth_required", str(e))
        elif not isinstance(e, RemoteToolError):
            await _mark(h.upstream_id, "error", str(e))
        raise
    return _flatten_result(res)


async def _mark(upstream_id: str, status: str, error: str | None) -> None:
    async with sessionmaker()() as db:
        with tenancy.unrestricted():
            u = await db.get(McpUpstream, upstream_id)
            if u is not None:
                u.status, u.last_error, u.last_checked_at = status, (error or "")[:2000] or None, datetime.now(UTC)
                await db.commit()
    events.publish("hub.changed", {"id": upstream_id})


# ------------------------------------------------------------------ output
def upstream_out(u: McpUpstream, env_names: dict[str, str] | None = None, *, full: bool = False) -> dict[str, Any]:
    sec = secrets_of(u)
    cfg = u.config or {}
    tools = u.tools or []
    states = [tool_state(u, t) for t in tools]
    out = {
        "id": u.id, "name": u.name, "namespace": u.namespace, "description": u.description,
        "customer_id": u.environment_id, "customer_name": (env_names or {}).get(u.environment_id or ""),
        "transport": u.transport, "url": u.url, "agent_node_id": u.agent_node_id, "tls_mode": u.tls_mode,
        "fingerprint": u.fingerprint, "command": u.command, "args": u.args or [], "auth_kind": u.auth_kind,
        "enabled": u.enabled, "status": u.status if u.enabled else "disabled", "last_error": u.last_error,
        "last_checked_at": u.last_checked_at, "latency_ms": u.latency_ms, "server_info": u.server_info or {},
        "preset": cfg.get("preset"), "timeout": cfg.get("timeout") or 60, "token_env": cfg.get("token_env"),
        "headers_public": cfg.get("headers_public") or {}, "env_public": cfg.get("env_public") or {},
        "secret_header_names": sorted((sec.get("headers") or {}).keys()),
        "secret_env_names": sorted((sec.get("env") or {}).keys()),
        "has_token": bool(sec.get("token")),
        "oauth": {"client_id": cfg.get("oauth_client_id"), "scopes": cfg.get("oauth_scopes"),
                  "has_client_secret": bool(sec.get("oauth_client_secret")),
                  "connected": bool((sec.get("oauth") or {}).get("access_token")),
                  "expires_at": (sec.get("oauth") or {}).get("expires_at"),
                  "account": (sec.get("oauth") or {}).get("account")},
        "tool_count": len(tools), "active_count": states.count("active"),
        "pending_count": states.count("new") + states.count("changed"),
        "created_at": u.created_at, "updated_at": u.updated_at, "created_by": u.created_by,
    }
    if full:
        out["instructions"] = u.instructions
        out["tools"] = [{"name": t["name"], "exposed": t["exposed"], "title": t.get("title"),
                         "description": t.get("description"), "annotations": t.get("annotations") or {},
                         "input_schema": t.get("input_schema") or {}, "state": tool_state(u, t),
                         "risk": tool_risk(u, t), "suggested_risk": t.get("suggested_risk"),
                         "risk_overridden": bool(((u.tool_settings or {}).get(t["name"]) or {}).get("risk")),
                         "first_seen": t.get("first_seen")} for t in tools]
    return out


async def env_names(db: AsyncSession) -> dict[str, str]:
    with tenancy.unrestricted():
        return {e.id: e.name for e in (await db.execute(select(Environment))).scalars()}


# ------------------------------------------------------------------ OAuth (authorization code + PKCE)
async def _get_json(url: str) -> dict[str, Any] | None:
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as c:
            r = await c.get(url, headers={"Accept": "application/json"})
        if r.status_code == 200:
            data = r.json()
            return data if isinstance(data, dict) else None
    except (httpx.HTTPError, ValueError):
        return None
    return None


async def discover_oauth(url: str) -> dict[str, Any]:
    """Find the authorization server of an MCP server (RFC 9728, then RFC 8414 / OpenID)."""
    p = urlsplit(url)
    origin = f"{p.scheme}://{p.netloc}"
    path = p.path.rstrip("/")
    issuer = origin
    for cand in ([f"{origin}/.well-known/oauth-protected-resource{path}"] if path else []) + \
            [f"{origin}/.well-known/oauth-protected-resource"]:
        meta = await _get_json(cand)
        if meta and meta.get("authorization_servers"):
            issuer = str(meta["authorization_servers"][0]).rstrip("/")
            break
    ip = urlsplit(issuer)
    ipath = ip.path.rstrip("/")
    iorigin = f"{ip.scheme}://{ip.netloc}"
    for cand in [f"{iorigin}/.well-known/oauth-authorization-server{ipath}",
                 f"{iorigin}/.well-known/openid-configuration{ipath}",
                 f"{issuer}/.well-known/openid-configuration"]:
        meta = await _get_json(cand)
        if meta and meta.get("authorization_endpoint") and meta.get("token_endpoint"):
            return {"issuer": issuer, "authorization_endpoint": meta["authorization_endpoint"],
                    "token_endpoint": meta["token_endpoint"],
                    "registration_endpoint": meta.get("registration_endpoint"),
                    "scopes_supported": meta.get("scopes_supported")}
    raise HubError("could not find the OAuth server of this MCP server; enter the client id and the "
                   "authorization and token addresses yourself", "oauth_discovery")


async def oauth_start(db: AsyncSession, u: McpUpstream, redirect_uri: str) -> str:
    if u.transport == "stdio" or not u.url:
        raise HubConfigError("OAuth needs an http(s) MCP server", "bad_config")
    cfg = dict(u.config or {})
    sec = secrets_of(u)
    if cfg.get("oauth_authorize_url") and cfg.get("oauth_token_url"):
        meta = {"authorization_endpoint": cfg["oauth_authorize_url"], "token_endpoint": cfg["oauth_token_url"],
                "registration_endpoint": None}
    else:
        meta = await discover_oauth(u.url)
    client_id = cfg.get("oauth_client_id")
    if not client_id:
        reg = meta.get("registration_endpoint")
        if not reg:
            raise HubConfigError("this OAuth server does not register clients itself; create an OAuth app there "
                                 "and enter its client id (and secret)", "oauth_client_needed")
        try:
            async with httpx.AsyncClient(timeout=15) as c:
                r = await c.post(reg, json={"client_name": "Stackpilot MCP hub", "redirect_uris": [redirect_uri],
                                            "grant_types": ["authorization_code", "refresh_token"],
                                            "response_types": ["code"], "token_endpoint_auth_method": "none"})
            data = r.json()
        except (httpx.HTTPError, ValueError) as e:
            raise HubError(f"client registration failed: {e}", "oauth_error") from e
        if r.status_code >= 400 or not data.get("client_id"):
            raise HubError(f"client registration failed (HTTP {r.status_code})", "oauth_error")
        client_id = data["client_id"]
        cfg["oauth_client_id"] = client_id
        cfg["oauth_registered"] = True
        if data.get("client_secret"):
            sec["oauth_client_secret"] = data["client_secret"]
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(24)
    sec["oauth_pending"] = {"state": state, "verifier": verifier, "redirect_uri": redirect_uri,
                            "token_endpoint": meta["token_endpoint"],
                            "expires": (datetime.now(UTC) + timedelta(minutes=15)).isoformat()}
    cfg["oauth_state_hash"] = hashlib.sha256(state.encode()).hexdigest()
    u.config = cfg
    set_secrets(u, sec)
    await db.flush()
    params = {"response_type": "code", "client_id": client_id, "redirect_uri": redirect_uri, "state": state,
              "code_challenge": challenge, "code_challenge_method": "S256", "resource": u.url}
    if cfg.get("oauth_scopes"):
        params["scope"] = cfg["oauth_scopes"]
    sep = "&" if "?" in meta["authorization_endpoint"] else "?"
    return f"{meta['authorization_endpoint']}{sep}{urlencode(params)}"


async def _token_request(token_url: str, data: dict[str, str], client_id: str, client_secret: str | None) -> dict:
    form = {**data, "client_id": client_id}
    auth = None
    if client_secret:
        auth = (client_id, client_secret)
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.post(token_url, data=form, auth=auth, headers={"Accept": "application/json"})
        body = r.json()
    except (httpx.HTTPError, ValueError) as e:
        raise HubError(f"the OAuth server did not answer: {e}", "oauth_error") from e
    if r.status_code >= 400 or not body.get("access_token"):
        msg = body.get("error_description") or body.get("error") or f"HTTP {r.status_code}"
        raise HubError(f"OAuth: {msg}", "oauth_error")
    return body


def _store_tokens(sec: dict[str, Any], body: dict[str, Any], token_url: str) -> None:
    old = sec.get("oauth") or {}
    expires = None
    if body.get("expires_in"):
        expires = (datetime.now(UTC) + timedelta(seconds=int(body["expires_in"]))).isoformat()
    sec["oauth"] = {"access_token": body["access_token"],
                    "refresh_token": body.get("refresh_token") or old.get("refresh_token"),
                    "expires_at": expires, "token_endpoint": token_url, "scope": body.get("scope"),
                    "account": old.get("account")}


async def oauth_finish(db: AsyncSession, state: str, code: str) -> McpUpstream:
    h = hashlib.sha256(state.encode()).hexdigest()
    with tenancy.unrestricted():
        rows = list((await db.execute(select(McpUpstream))).scalars())
    u = next((r for r in rows if (r.config or {}).get("oauth_state_hash") == h), None)
    if u is None:
        raise HubConfigError("unknown or expired sign-in", "oauth_state")
    sec = secrets_of(u)
    pend = sec.get("oauth_pending") or {}
    if pend.get("state") != state or datetime.fromisoformat(pend["expires"]) < datetime.now(UTC):
        raise HubConfigError("unknown or expired sign-in", "oauth_state")
    cfg = dict(u.config or {})
    body = await _token_request(pend["token_endpoint"], {
        "grant_type": "authorization_code", "code": code, "redirect_uri": pend["redirect_uri"],
        "code_verifier": pend["verifier"], "resource": u.url or ""}, cfg["oauth_client_id"],
        sec.get("oauth_client_secret"))
    _store_tokens(sec, body, pend["token_endpoint"])
    sec.pop("oauth_pending", None)
    cfg.pop("oauth_state_hash", None)
    u.config = cfg
    set_secrets(u, sec)
    u.status = "unknown"
    forget(u.id)
    await db.flush()
    return u


async def oauth_access_token(db: AsyncSession, u: McpUpstream) -> str:
    sec = secrets_of(u)
    o = sec.get("oauth") or {}
    if not o.get("access_token"):
        raise HubError("sign in to this MCP server first (OAuth)", "oauth_required")
    exp = o.get("expires_at")
    if exp and datetime.fromisoformat(exp) < datetime.now(UTC) + timedelta(seconds=60):
        if not o.get("refresh_token"):
            raise HubError("the OAuth sign-in expired; sign in again", "oauth_required")
        try:
            body = await _token_request(o["token_endpoint"], {"grant_type": "refresh_token",
                                                              "refresh_token": o["refresh_token"],
                                                              "resource": u.url or ""},
                                        (u.config or {}).get("oauth_client_id") or "", sec.get("oauth_client_secret"))
        except HubError as e:
            raise HubError(f"the OAuth sign-in could not be renewed ({e}); sign in again", "oauth_required") from e
        _store_tokens(sec, body, o["token_endpoint"])
        set_secrets(u, sec)
        await db.flush()
        o = sec["oauth"]
    return o["access_token"]
