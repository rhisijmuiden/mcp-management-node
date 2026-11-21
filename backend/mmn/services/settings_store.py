"""Key/value application settings stored in the database."""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from mmn.models import Setting

DEFAULTS: dict[str, dict[str, Any]] = {
    "general": {
        "app_name": "MCP Management Node",
        "base_url": "",
        "default_language": "en",
    },
    "mail": {
        "enabled": False,
        "host": "",
        "port": 587,
        "security": "starttls",  # none | starttls | tls
        "username": "",
        "password_enc": None,
        "from_name": "MCP Management Node",
        "from_address": "",
        "reply_to": "",
        "verify_tls": True,
        "min_tls": "1.2",
        "timeout": 20,
        "dkim_enabled": False,
        "dkim_domain": "",
        "dkim_selector": "",
        "dkim_key_enc": None,
    },
    "notifications": {
        "backup_failure_to": "",
        "backup_success_to": "",
        "update_to": "",
    },
    "updates": {
        "channel": "production",  # production | test
        "auto_check": True,
        "last_check": None,
        "latest": None,
    },
    "proxy": {
        "mode": "direct",  # direct | proxy
        "trusted_proxies": [],
        "server_name": "_",
        "http_port": 80,
        "https_port": 443,
    },
}


async def get_setting(db: AsyncSession, key: str) -> dict[str, Any]:
    row = await db.get(Setting, key)
    value = dict(DEFAULTS.get(key, {}))
    if row is not None:
        value.update(row.value or {})
    return value


async def set_setting(db: AsyncSession, key: str, value: dict[str, Any]) -> dict[str, Any]:
    row = await db.get(Setting, key)
    merged = dict(DEFAULTS.get(key, {}))
    if row is not None:
        merged.update(row.value or {})
    merged.update(value)
    if row is None:
        db.add(Setting(key=key, value=merged))
    else:
        row.value = merged
    await db.flush()
    return merged
