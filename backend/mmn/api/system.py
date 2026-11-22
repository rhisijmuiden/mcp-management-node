from __future__ import annotations

import platform
import time
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from mmn.api.deps import client_ip, current_user, require_admin
from mmn.config import get_settings
from mmn.db import get_db
from mmn.models import User
from mmn.services import proxy_config, updates
from mmn.services.audit import audit
from mmn.services.settings_store import get_setting
from mmn.version import get_build_info

router = APIRouter(prefix="/api/system", tags=["system"])
STARTED = time.time()


async def system_info(db: AsyncSession) -> dict:
    b = get_build_info()
    pg = (await db.execute(text("SHOW server_version"))).scalar_one()
    try:
        rev = (await db.execute(text("SELECT version_num FROM alembic_version"))).scalar_one_or_none()
    except Exception:  # noqa: BLE001
        rev = None
    cfg = await get_setting(db, "updates")
    return {
        "name": "Stackpilot", "version": b.version, "tag": b.tag, "build_channel": b.channel,
        "commit": b.commit, "built_at": b.built_at, "dev": b.dev, "channel": cfg["channel"],
        "python": platform.python_version(), "postgres": pg, "db_revision": rev,
        "os": _os_name(), "uptime_seconds": int(time.time() - STARTED),
        "updater_available": updates.updater_available(), "repo": get_settings().github_repo,
        "last_check": cfg.get("last_check"), "latest": cfg.get("latest"),
        "update_available": cfg.get("update_available", False),
    }


def _os_name() -> str:
    try:
        data = dict(line.split("=", 1) for line in open("/etc/os-release").read().splitlines() if "=" in line)
        return data.get("PRETTY_NAME", "").strip('"') or platform.platform()
    except OSError:
        return platform.platform()


@router.get("/info")
async def info(db: AsyncSession = Depends(get_db), _: User = Depends(current_user)):
    return await system_info(db)


@router.post("/updates/check")
async def check(db: AsyncSession = Depends(get_db), _: User = Depends(require_admin)):
    try:
        res = await updates.check_for_updates(db)
    except (updates.UpdateError, Exception) as e:  # noqa: BLE001
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail=str(e) or type(e).__name__) from e
    await db.commit()
    return res


class ChannelIn(BaseModel):
    channel: Literal["production", "test"]


@router.put("/updates/channel")
async def channel(body: ChannelIn, request: Request, db: AsyncSession = Depends(get_db),
                  admin: User = Depends(require_admin)):
    await updates.set_channel(db, body.channel)
    await audit(db, "update.channel", user=admin, target=body.channel, ip=client_ip(request))
    await db.commit()
    try:
        res = await updates.check_for_updates(db)
        await db.commit()
        return res
    except Exception as e:  # noqa: BLE001
        return {"channel": body.channel, "error": str(e)}


class ApplyIn(BaseModel):
    tag: str


@router.post("/updates/apply", status_code=202)
async def apply(body: ApplyIn, request: Request, db: AsyncSession = Depends(get_db),
                admin: User = Depends(require_admin)):
    cfg = await get_setting(db, "updates")
    try:
        updates.request_update(body.tag, cfg["channel"], admin.username)
    except updates.UpdateError as e:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(e)) from e
    await audit(db, "update.apply", user=admin, target=body.tag, ip=client_ip(request))
    await db.commit()
    return updates.read_status()


@router.get("/updates/status")
async def update_status(_: User = Depends(current_user)):
    return updates.read_status()


# ---------- reverse proxy / web server ----------
@router.get("/proxy")
async def get_proxy(request: Request, db: AsyncSession = Depends(get_db), _: User = Depends(require_admin)):
    cfg = await get_setting(db, "proxy")
    return {
        **cfg, "base_path": get_settings().base_path, "apply_available": updates.updater_available(),
        "apply_status": proxy_config.read_status(),
        "seen": {"client_ip": client_ip(request), "scheme": request.url.scheme,
                 "host": request.headers.get("host"),
                 "forwarded_for": request.headers.get("x-forwarded-for"),
                 "forwarded_proto": request.headers.get("x-forwarded-proto")},
    }


@router.put("/proxy")
async def put_proxy(body: proxy_config.ProxyIn, request: Request, db: AsyncSession = Depends(get_db),
                    admin: User = Depends(require_admin)):
    try:
        value = await proxy_config.save_and_apply(db, body)
    except proxy_config.ProxyConfigError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(e)) from e
    await audit(db, "system.proxy", user=admin, ip=client_ip(request), mode=body.mode)
    await db.commit()
    return value
