"""GitHub deployments into LXC containers, and the GitHub webhook."""

from __future__ import annotations

import json

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from mmn.api.deps import client_ip, current_user, require_admin
from mmn.api.network import run
from mmn.api.proxmox import _err
from mmn.db import get_db
from mmn.models import User
from mmn.services import deploy, guests, nodes
from mmn.services.agent_hub import AgentError
from mmn.services.audit import audit
from mmn.services.settings_store import get_setting

router = APIRouter(tags=["deploy"])


def _gh(e: Exception) -> HTTPException:
    return HTTPException(status.HTTP_502_BAD_GATEWAY, detail=str(e))


async def _base(db: AsyncSession) -> str:
    return (await get_setting(db, "general")).get("base_url") or ""


# ------------------------------------------------------------------ GitHub connection
@router.get("/api/github")
async def github(db: AsyncSession = Depends(get_db), _: User = Depends(require_admin)):
    return await deploy.github_status(db)


class TokenIn(BaseModel):
    token: str | None = Field(default=None, max_length=500)


@router.put("/api/github")
async def set_token(body: TokenIn, request: Request, db: AsyncSession = Depends(get_db),
                    admin: User = Depends(require_admin)):
    try:
        out = await deploy.set_github_token(db, (body.token or "").strip() or None)
    except (deploy.GitHubError, httpx.HTTPError) as e:
        raise _gh(e) from e
    await audit(db, "github.token", user=admin, ip=client_ip(request), login=out.get("login"),
                removed=not body.token)
    await db.commit()
    return out


@router.get("/api/github/repos")
async def repos(q: str | None = None, db: AsyncSession = Depends(get_db), _: User = Depends(require_admin)):
    try:
        return await deploy.list_repos(db, q)
    except (deploy.GitHubError, httpx.HTTPError) as e:
        raise _gh(e) from e


@router.get("/api/github/branches")
async def branches(repo: str, db: AsyncSession = Depends(get_db), _: User = Depends(require_admin)):
    try:
        return await deploy.list_branches(db, repo)
    except (deploy.GitHubError, httpx.HTTPError) as e:
        raise _gh(e) from e


@router.get("/api/github/detect")
async def detect(repo: str, branch: str, db: AsyncSession = Depends(get_db), _: User = Depends(require_admin)):
    try:
        return await deploy.detect(db, repo, branch)
    except (deploy.GitHubError, httpx.HTTPError) as e:
        raise _gh(e) from e


@router.get("/api/deployments/presets")
async def presets(_: User = Depends(current_user)):
    return deploy.PRESETS


# ------------------------------------------------------------------ deployments
@router.get("/api/deployments")
async def list_deployments(db: AsyncSession = Depends(get_db), _: User = Depends(current_user)):
    base = await _base(db)
    return [deploy.deployment_out(d, base) for d in await deploy.list_deployments(db)]


@router.post("/api/deployments", status_code=201)
async def create(body: deploy.DeploymentIn, request: Request, db: AsyncSession = Depends(get_db),
                 admin: User = Depends(require_admin)):
    d = await run(db, request, admin, "deploy.save", body.name, deploy.save_deployment(db, body, user=admin.username),
                  repo=body.repo)
    return deploy.deployment_out(d, await _base(db))


class WithContainerIn(BaseModel):
    deployment: deploy.DeploymentIn
    container: guests.CreateCtIn


@router.post("/api/deployments/with-container", status_code=202)
async def create_with_container(body: WithContainerIn, request: Request, db: AsyncSession = Depends(get_db),
                                admin: User = Depends(require_admin)):
    d, job = await run(db, request, admin, "deploy.create_container", body.deployment.name,
                       deploy.create_with_container(db, body.deployment, body.container, requested_by=admin.username),
                       repo=body.deployment.repo, node=body.container.node_id)
    return {"deployment": deploy.deployment_out(d, await _base(db)), "job": nodes.job_out(job, with_output=False)}


@router.get("/api/deployments/{dep_id}")
async def get_one(dep_id: str, db: AsyncSession = Depends(get_db), _: User = Depends(current_user)):
    try:
        d = await deploy.get_deployment(db, dep_id)
    except nodes.NodeError as e:
        raise _err(e) from e
    return {**deploy.deployment_out(d, await _base(db)), "runs": await deploy.list_runs(db, d.id)}


@router.get("/api/deployments/{dep_id}/webhook")
async def webhook_info(dep_id: str, db: AsyncSession = Depends(get_db), _: User = Depends(require_admin)):
    try:
        d = await deploy.get_deployment(db, dep_id)
    except nodes.NodeError as e:
        raise _err(e) from e
    out = deploy.deployment_out(d, await _base(db))
    return {"url": out["webhook_url"], "secret": deploy.webhook_secret(d), "content_type": "application/json",
            "events": ["push"]}


@router.put("/api/deployments/{dep_id}")
async def update(dep_id: str, body: deploy.DeploymentIn, request: Request, db: AsyncSession = Depends(get_db),
                 admin: User = Depends(require_admin)):
    try:
        d = await deploy.get_deployment(db, dep_id)
    except nodes.NodeError as e:
        raise _err(e) from e
    d = await run(db, request, admin, "deploy.save", body.name, deploy.save_deployment(db, body, d), repo=body.repo)
    return deploy.deployment_out(d, await _base(db))


@router.delete("/api/deployments/{dep_id}", status_code=204)
async def delete(dep_id: str, request: Request, db: AsyncSession = Depends(get_db),
                 admin: User = Depends(require_admin)):
    try:
        d = await deploy.get_deployment(db, dep_id)
    except nodes.NodeError as e:
        raise _err(e) from e
    await db.delete(d)
    await audit(db, "deploy.delete", user=admin, target=d.name, ip=client_ip(request))
    await db.commit()


class RunIn(BaseModel):
    run_setup: bool = False


@router.post("/api/deployments/{dep_id}/run", status_code=202)
async def start(dep_id: str, body: RunIn, request: Request, db: AsyncSession = Depends(get_db),
                admin: User = Depends(require_admin)):
    try:
        d = await deploy.get_deployment(db, dep_id)
    except nodes.NodeError as e:
        raise _err(e) from e
    r = await run(db, request, admin, "deploy.run", d.name,
                  deploy.start_run(d.id, requested_by=admin.username, run_setup=body.run_setup),
                  branch=deploy.branch_of(d))
    return deploy.run_out(r)


@router.get("/api/deployment-runs")
async def runs(deployment_id: str | None = None, db: AsyncSession = Depends(get_db), _: User = Depends(current_user)):
    return await deploy.list_runs(db, deployment_id)


# ------------------------------------------------------------------ webhook (no login: HMAC signature)
@router.post("/api/hooks/github/{dep_id}", include_in_schema=False)
async def github_hook(dep_id: str, request: Request):
    body = await request.body()
    if len(body) > 5 * 1024 * 1024:
        return JSONResponse({"detail": "too large"}, status_code=413)
    try:
        payload = json.loads(body or b"{}")
    except ValueError:
        return JSONResponse({"detail": "invalid JSON"}, status_code=400)
    try:
        code, out = await deploy.handle_webhook(dep_id, request.headers.get("x-github-event"),
                                                request.headers.get("x-hub-signature-256"), body,
                                                payload if isinstance(payload, dict) else {})
    except AgentError as e:
        code, out = 409, {"detail": str(e)}
    return JSONResponse(out, status_code=code)
