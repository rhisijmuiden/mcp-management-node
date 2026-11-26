"""Software library (v0.27.0): GitHub accounts and repositories with rights per customer."""

from __future__ import annotations

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from mmn.api.deps import client_ip, current_user, require_admin
from mmn.db import get_db
from mmn.models import GithubAccount, User
from mmn.services import github_library as gl
from mmn.services.audit import audit

router = APIRouter(tags=["software"])


def _gh(e: Exception) -> HTTPException:
    return HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(e))


async def _account(db: AsyncSession, aid: str) -> GithubAccount:
    a = await db.get(GithubAccount, aid)
    if a is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="GitHub account not found")
    return a


async def _repo(db: AsyncSession, rid: str):
    try:
        return await gl.get_repo(db, rid)
    except gl.GitHubError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(e)) from e


# ------------------------------------------------------------------ accounts
@router.get("/api/github/accounts")
async def accounts(db: AsyncSession = Depends(get_db), _: User = Depends(require_admin)):
    out = [gl.account_out(a) for a in await gl.list_accounts(db)]
    await db.commit()
    return out


class AccountIn(BaseModel):
    name: str | None = Field(default=None, max_length=100)
    token: str = Field(min_length=1, max_length=500)


@router.post("/api/github/accounts", status_code=201)
async def add_account(body: AccountIn, request: Request, db: AsyncSession = Depends(get_db),
                      admin: User = Depends(require_admin)):
    try:
        a = await gl.add_account(db, body.name, body.token)
    except (gl.GitHubError, httpx.HTTPError) as e:
        raise _gh(e) from e
    await audit(db, "github.account_add", user=admin, ip=client_ip(request), target=a.name, login=a.login)
    await db.commit()
    return gl.account_out(a)


class AccountUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=100)
    token: str | None = Field(default=None, max_length=500)


@router.put("/api/github/accounts/{aid}")
async def update_account(aid: str, body: AccountUpdate, request: Request, db: AsyncSession = Depends(get_db),
                         admin: User = Depends(require_admin)):
    a = await _account(db, aid)
    try:
        if body.token and body.token.strip():
            await gl.replace_token(db, a, body.token)
    except (gl.GitHubError, httpx.HTTPError) as e:
        raise _gh(e) from e
    if body.name and body.name.strip():
        a.name = body.name.strip()
    await audit(db, "github.account_update", user=admin, ip=client_ip(request), target=a.name,
                token_replaced=bool(body.token))
    await db.commit()
    return gl.account_out(a)


@router.post("/api/github/accounts/{aid}/check")
async def check_account(aid: str, db: AsyncSession = Depends(get_db), _: User = Depends(require_admin)):
    a = await gl.check_account(db, await _account(db, aid))
    await db.commit()
    return gl.account_out(a)


@router.get("/api/github/accounts/{aid}/owners")
async def account_owners(aid: str, db: AsyncSession = Depends(get_db), _: User = Depends(require_admin)):
    try:
        return await gl.owners(db, await _account(db, aid))
    except (gl.GitHubError, httpx.HTTPError) as e:
        raise _gh(e) from e


@router.delete("/api/github/accounts/{aid}", status_code=204)
async def delete_account(aid: str, request: Request, db: AsyncSession = Depends(get_db),
                         admin: User = Depends(require_admin)):
    a = await _account(db, aid)
    await db.delete(a)
    await audit(db, "github.account_delete", user=admin, ip=client_ip(request), target=a.name)
    await db.commit()


@router.get("/api/github/remote")
async def remote(account: str | None = None, q: str | None = None, db: AsyncSession = Depends(get_db),
                 _: User = Depends(require_admin)):
    try:
        return await gl.remote_repos(db, account, q)
    except (gl.GitHubError, httpx.HTTPError) as e:
        raise _gh(e) from e


# ------------------------------------------------------------------ library
@router.get("/api/repos")
async def library(db: AsyncSession = Depends(get_db), _: User = Depends(current_user)):
    return await gl.library(db)


@router.post("/api/repos/import", status_code=201)
async def import_repos(body: gl.ImportIn, request: Request, db: AsyncSession = Depends(get_db),
                       admin: User = Depends(require_admin)):
    try:
        repos = await gl.import_repos(db, body, admin.username)
    except (gl.GitHubError, httpx.HTTPError) as e:
        raise _gh(e) from e
    await audit(db, "repo.import", user=admin, ip=client_ip(request), repos=[r.full_name for r in repos])
    await db.commit()
    return [await gl.repo_view(db, r) for r in repos]


@router.post("/api/repos/create", status_code=201)
async def create_repo(body: gl.CreateIn, request: Request, db: AsyncSession = Depends(get_db),
                      admin: User = Depends(require_admin)):
    try:
        r = await gl.create_repo(db, body, admin.username)
    except (gl.GitHubError, httpx.HTTPError) as e:
        raise _gh(e) from e
    await audit(db, "repo.create", user=admin, ip=client_ip(request), target=r.full_name, private=r.private)
    await db.commit()
    return await gl.repo_view(db, r)


@router.put("/api/repos/{rid}")
async def update_repo(rid: str, body: gl.RepoIn, request: Request, db: AsyncSession = Depends(get_db),
                      admin: User = Depends(require_admin)):
    r = await _repo(db, rid)
    try:
        await gl.update_repo(db, r, body)
    except gl.GitHubError as e:
        raise _gh(e) from e
    await audit(db, "repo.update", user=admin, ip=client_ip(request), target=r.full_name, kind=r.kind)
    await db.commit()
    return await gl.repo_view(db, r)


class AccessBody(BaseModel):
    customers: list[gl.AccessIn] = Field(default_factory=list, max_length=500)


@router.put("/api/repos/{rid}/access")
async def set_access(rid: str, body: AccessBody, request: Request, db: AsyncSession = Depends(get_db),
                     admin: User = Depends(require_admin)):
    r = await _repo(db, rid)
    try:
        await gl.set_access(db, r, body.customers)
    except gl.GitHubError as e:
        raise _gh(e) from e
    await audit(db, "repo.access", user=admin, ip=client_ip(request), target=r.full_name,
                customers=[c.customer_id for c in body.customers])
    await db.commit()
    return await gl.repo_view(db, r)


@router.delete("/api/repos/{rid}", status_code=204)
async def delete_repo(rid: str, request: Request, db: AsyncSession = Depends(get_db),
                      admin: User = Depends(require_admin)):
    """Removes the repository from the library only; nothing changes on GitHub."""
    r = await _repo(db, rid)
    await db.delete(r)
    await audit(db, "repo.remove", user=admin, ip=client_ip(request), target=r.full_name)
    await db.commit()
