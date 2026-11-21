from __future__ import annotations

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from mmn.config import get_settings
from mmn.services.settings_store import get_setting


async def public_base_url(db: AsyncSession, request: Request | None = None) -> str:
    """The URL people use to reach the app, e.g. https://pve.example.nl/proxmox (no trailing slash)."""
    general = await get_setting(db, "general")
    if general.get("base_url"):
        return general["base_url"].rstrip("/")
    if request is None:
        return ""
    base = get_settings().base_path
    host = request.headers.get("host", "localhost")
    return f"{request.url.scheme}://{host}{'' if base == '/' else base}"
