"""OAuth 2.1 authorization server for MCP clients (Claude, ChatGPT, Copilot, ...).

* Discovery: RFC 9728 protected-resource metadata and RFC 8414 authorization-server metadata.
* Dynamic client registration (RFC 7591) for public clients.
* Authorization code flow with PKCE (S256 only). The user approves on a consent page in the app,
  and chooses which scopes the AI client gets.
* Access tokens are API tokens of kind "oauth"; refresh tokens rotate on every use.
"""

from __future__ import annotations

import base64
import hashlib
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode, urlparse

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mmn.api.deps import client_ip, current_user
from mmn.api.tokens import ADMIN_ONLY_SCOPES, SCOPES, TOKEN_PREFIX
from mmn.db import get_db
from mmn.models import ApiToken, OAuthClient, OAuthCode, Role, User
from mmn.security import hash_token, new_token
from mmn.services.audit import audit
from mmn.services.links import public_base_url

router = APIRouter(tags=["oauth"])

ACCESS_TTL = timedelta(hours=8)
REFRESH_TTL = timedelta(days=90)
CODE_TTL = timedelta(minutes=10)


def _err(error: str, desc: str, code: int = 400) -> JSONResponse:
    return JSONResponse({"error": error, "error_description": desc}, status_code=code,
                        headers={"Cache-Control": "no-store"})


async def _base(db: AsyncSession, request: Request) -> str:
    return await public_base_url(db, request)


# ------------------------------------------------------------------ discovery
async def resource_metadata(request: Request, db: AsyncSession = Depends(get_db)):
    base = await _base(db, request)
    return {"resource": f"{base}/mcp", "authorization_servers": [base],
            "scopes_supported": list(SCOPES), "bearer_methods_supported": ["header"],
            "resource_name": "Stackpilot"}


async def as_metadata(request: Request, db: AsyncSession = Depends(get_db)):
    base = await _base(db, request)
    return {
        "issuer": base,
        "authorization_endpoint": f"{base}/oauth/authorize",
        "token_endpoint": f"{base}/oauth/token",
        "registration_endpoint": f"{base}/oauth/register",
        "revocation_endpoint": f"{base}/oauth/revoke",
        "scopes_supported": list(SCOPES),
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
        "service_documentation": f"{base}/help/mcp",
    }


for path in ("/.well-known/oauth-protected-resource", "/.well-known/oauth-protected-resource/{rest:path}"):
    router.add_api_route(path, resource_metadata, methods=["GET"], include_in_schema=False)
for path in ("/.well-known/oauth-authorization-server", "/.well-known/oauth-authorization-server/{rest:path}",
             "/.well-known/openid-configuration"):
    router.add_api_route(path, as_metadata, methods=["GET"], include_in_schema=False)


# ------------------------------------------------------------------ registration
class RegisterIn(BaseModel):
    redirect_uris: list[str] = Field(min_length=1, max_length=10)
    client_name: str | None = Field(default=None, max_length=200)
    client_uri: str | None = Field(default=None, max_length=500)
    token_endpoint_auth_method: str | None = "none"
    grant_types: list[str] | None = None
    response_types: list[str] | None = None
    scope: str | None = None


def _valid_redirect(uri: str) -> bool:
    u = urlparse(uri)
    if u.fragment:
        return False
    if u.scheme == "https" and u.netloc:
        return True
    if u.scheme == "http" and u.hostname in ("localhost", "127.0.0.1", "::1"):
        return True
    # Native apps (e.g. VS Code) may use a private-use scheme such as vscode://...
    return bool(u.scheme) and u.scheme not in ("http", "javascript", "data", "file", "vbscript", "about")


@router.post("/oauth/register", status_code=201, include_in_schema=False)
async def register(body: RegisterIn, request: Request, db: AsyncSession = Depends(get_db)):
    bad = [u for u in body.redirect_uris if not _valid_redirect(u)]
    if bad:
        return _err("invalid_redirect_uri", f"redirect URI not allowed: {bad[0]}")
    client = OAuthClient(client_id="mmnc_" + new_token(18), client_name=(body.client_name or "MCP client")[:200],
                         redirect_uris=body.redirect_uris, client_uri=body.client_uri)
    db.add(client)
    await audit(db, "oauth.register", source="mcp", target=client.client_name, ip=client_ip(request),
                client_id=client.client_id)
    await db.commit()
    return JSONResponse({
        "client_id": client.client_id, "client_name": client.client_name,
        "redirect_uris": client.redirect_uris, "token_endpoint_auth_method": "none",
        "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"],
        "client_id_issued_at": int(client.created_at.timestamp()) if client.created_at else None,
    }, status_code=201)


# ------------------------------------------------------------------ authorize
@router.get("/oauth/authorize", include_in_schema=False)
async def authorize(request: Request, db: AsyncSession = Depends(get_db)):
    q = request.query_params
    client = await db.get(OAuthClient, q.get("client_id", ""))
    redirect_uri = q.get("redirect_uri", "")
    if client is None:
        return _err("invalid_client", "unknown client_id")
    if redirect_uri not in client.redirect_uris:
        return _err("invalid_request", "redirect_uri is not registered for this client")
    if q.get("response_type") != "code" or q.get("code_challenge_method") != "S256" or not q.get("code_challenge"):
        sep = "&" if "?" in redirect_uri else "?"
        return RedirectResponse(f"{redirect_uri}{sep}" + urlencode({
            "error": "invalid_request", "error_description": "response_type=code with PKCE S256 is required",
            **({"state": q["state"]} if q.get("state") else {})}), status_code=302)
    base = await _base(db, request)
    return RedirectResponse(f"{base}/connect/consent?{request.url.query}", status_code=302)


@router.get("/api/oauth/clients/{client_id}")
async def client_info(client_id: str, db: AsyncSession = Depends(get_db), user: User = Depends(current_user)):
    client = await db.get(OAuthClient, client_id)
    if client is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    allowed = [s for s in SCOPES if user.role == Role.admin or s not in ADMIN_ONLY_SCOPES]
    return {"client_id": client.client_id, "client_name": client.client_name, "client_uri": client.client_uri,
            "redirect_hosts": sorted({urlparse(u).netloc or urlparse(u).scheme for u in client.redirect_uris}),
            "scopes": [{"scope": s, "description": SCOPES[s]} for s in allowed]}


class ApproveIn(BaseModel):
    client_id: str
    redirect_uri: str
    code_challenge: str = Field(min_length=43, max_length=128)
    state: str | None = Field(default=None, max_length=2000)
    resource: str | None = Field(default=None, max_length=500)
    scopes: list[str] = Field(default_factory=lambda: ["read"])
    # None = all customers; otherwise the AI app only sees these customers.
    customer_ids: list[str] | None = Field(default=None, max_length=500)
    approve: bool = True


@router.post("/api/oauth/approve")
async def approve(body: ApproveIn, request: Request, db: AsyncSession = Depends(get_db),
                  user: User = Depends(current_user)):
    client = await db.get(OAuthClient, body.client_id)
    if client is None or body.redirect_uri not in client.redirect_uris:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="invalid_client")
    sep = "&" if "?" in body.redirect_uri else "?"
    state = {"state": body.state} if body.state else {}
    if not body.approve:
        await audit(db, "oauth.deny", user=user, target=client.client_name, ip=client_ip(request))
        await db.commit()
        return {"redirect": f"{body.redirect_uri}{sep}" + urlencode({"error": "access_denied", **state})}
    scopes = sorted({s for s in body.scopes if s in SCOPES} | {"read"})
    if user.role != Role.admin:
        scopes = [s for s in scopes if s not in ADMIN_ONLY_SCOPES]
    from mmn.api.tokens import clean_customer_ids

    customer_ids = await clean_customer_ids(db, body.customer_ids)
    code = new_token(32)
    db.add(OAuthCode(code_hash=hash_token(code), client_id=client.client_id, user_id=user.id,
                     customer_ids=customer_ids,
                     redirect_uri=body.redirect_uri, scopes=scopes, code_challenge=body.code_challenge,
                     resource=body.resource, expires_at=datetime.now(UTC) + CODE_TTL))
    await audit(db, "oauth.approve", user=user, target=client.client_name, ip=client_ip(request), scopes=scopes)
    await db.commit()
    return {"redirect": f"{body.redirect_uri}{sep}" + urlencode({"code": code, **state})}


# ------------------------------------------------------------------ token
def _issue(db: AsyncSession, user_id: str, client: OAuthClient, scopes: list[str],
           customer_ids: list[str] | None = None) -> dict:
    access, refresh = TOKEN_PREFIX + new_token(32), "mmnr_" + new_token(32)
    now = datetime.now(UTC)
    db.add(ApiToken(user_id=user_id, name=client.client_name, token_hash=hash_token(access), prefix=access[:12],
                    scopes=scopes, kind="oauth", oauth_client_id=client.client_id, customer_ids=customer_ids,
                    expires_at=now + ACCESS_TTL, refresh_hash=hash_token(refresh),
                    refresh_expires_at=now + REFRESH_TTL))
    return {"access_token": access, "token_type": "Bearer", "expires_in": int(ACCESS_TTL.total_seconds()),
            "refresh_token": refresh, "scope": " ".join(scopes)}


@router.post("/oauth/token", include_in_schema=False)
async def token(request: Request, db: AsyncSession = Depends(get_db), grant_type: str = Form(...),
                code: str | None = Form(None), redirect_uri: str | None = Form(None),
                client_id: str | None = Form(None), code_verifier: str | None = Form(None),
                refresh_token: str | None = Form(None)):
    client = await db.get(OAuthClient, client_id or "")
    if client is None:
        return _err("invalid_client", "unknown client_id", 401)
    if grant_type == "authorization_code":
        row = await db.get(OAuthCode, hash_token(code or ""))
        if row is None or row.client_id != client.client_id or row.expires_at < datetime.now(UTC):
            return _err("invalid_grant", "code is invalid or expired")
        await db.delete(row)  # single use
        if row.redirect_uri != redirect_uri:
            await db.commit()
            return _err("invalid_grant", "redirect_uri mismatch")
        digest = base64.urlsafe_b64encode(hashlib.sha256((code_verifier or "").encode()).digest()).decode().rstrip("=")
        if digest != row.code_challenge:
            await db.commit()
            return _err("invalid_grant", "PKCE verification failed")
        result = _issue(db, row.user_id, client, row.scopes, row.customer_ids)
        await audit(db, "oauth.token", source="mcp", target=client.client_name, ip=client_ip(request))
        await db.commit()
        return JSONResponse(result, headers={"Cache-Control": "no-store"})
    if grant_type == "refresh_token":
        old = (await db.execute(select(ApiToken).where(ApiToken.refresh_hash == hash_token(refresh_token or ""))
                                )).scalar_one_or_none()
        if (old is None or old.revoked or old.oauth_client_id != client.client_id
                or (old.refresh_expires_at and old.refresh_expires_at < datetime.now(UTC))
                or not old.user.is_active):
            return _err("invalid_grant", "refresh token is invalid")
        old.revoked, old.refresh_hash = True, None
        result = _issue(db, old.user_id, client, list(old.scopes),
                        list(old.customer_ids) if old.customer_ids is not None else None)
        await db.commit()
        return JSONResponse(result, headers={"Cache-Control": "no-store"})
    return _err("unsupported_grant_type", grant_type)


@router.post("/oauth/revoke", include_in_schema=False)
async def revoke(db: AsyncSession = Depends(get_db), token: str = Form(...)):
    h = hash_token(token)
    tok = (await db.execute(select(ApiToken).where((ApiToken.token_hash == h) | (ApiToken.refresh_hash == h))
                            )).scalar_one_or_none()
    if tok is not None:
        tok.revoked = True
        await db.commit()
    return JSONResponse({}, status_code=200)
