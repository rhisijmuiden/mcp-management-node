"""Web server (Nginx) settings: direct HTTPS or behind a reverse proxy such as Zoraxy."""

from __future__ import annotations

import json
import os
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from mmn.config import get_settings
from mmn.nginx import ProxyIn, render_nginx
from mmn.services.settings_store import set_setting
from mmn.services.updates import updater_available

__all__ = ["ProxyIn", "ProxyConfigError", "save_and_apply", "read_status"]


class ProxyConfigError(Exception):
    pass


def status_file():
    return get_settings().run_dir / "system-status.json"


def read_status() -> dict[str, Any]:
    f = status_file()
    if not f.exists():
        return {"state": "idle"}
    try:
        return json.loads(f.read_text())
    except (OSError, ValueError):
        return {"state": "unknown"}


async def save_and_apply(db: AsyncSession, body: ProxyIn) -> dict[str, Any]:
    try:
        body.check()
        render_nginx(body)  # validate it renders
    except ValueError as e:
        raise ProxyConfigError(str(e)) from e
    value = await set_setting(db, "proxy", body.model_dump(exclude={"base_path"}))
    applied = False
    if updater_available():
        s = get_settings()
        s.run_dir.mkdir(parents=True, exist_ok=True)
        status_file().write_text(json.dumps({"state": "queued"}))
        req = s.run_dir / "system-request.json"
        tmp = req.with_suffix(".tmp")
        tmp.write_text(json.dumps({"action": "reconfigure", "config": body.model_dump()}))
        os.replace(tmp, req)
        applied = True
    return {**value, "base_path": body.base_path, "queued": applied}
