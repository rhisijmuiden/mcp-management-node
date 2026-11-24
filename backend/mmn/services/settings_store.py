"""Key/value application settings stored in the database."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from mmn.models import Setting
from mmn.version import get_build_info

DEFAULTS: dict[str, dict[str, Any]] = {
    "general": {
        "app_name": "Stackpilot",
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
        "from_name": "Stackpilot",
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
        "node_update_to": "",
    },
    "updates": {
        "channel": "production",  # production | test
        "auto_check": True,
        "last_check": None,
        "latest": None,
    },
    "agents": {
        # Address agents connect to (host or host:port), e.g. mmn.example.nl:8443. Empty: derived
        # from the public address in General settings.
        "address": "",
        "port": 8443,
        "auto_update": True,
        "updates_check_hours": 6,
    },
    "templates": {
        "storage": "local",  # storage that receives library templates when a template sets none
    },
    "community": {
        "repo": "community-scripts/ProxmoxVE",
        "branch": "main",
        "raw_base": "https://raw.githubusercontent.com/community-scripts/ProxmoxVE/main",
        "sync_cron": "15 3 * * *",
    },
    "schedules": {
        "timezone": "Europe/Amsterdam",  # time zone of scheduled tasks
    },
    "ai_gateway": {
        # Kill switch: refuse every AI (MCP) call that is not read-only.
        "paused": False,
        "pause_reads": False,
        "pause_note": "",
        "paused_by": None,
        "paused_at": None,
        # How long an MCP call that needs approval waits for a decision before it answers "pending".
        "approval_wait_seconds": 25,
        "approval_expiry_hours": 24,
        # Notification channels for new approval requests (see services/ai_notify.py).
        "notify_app": True,
        "notify_mail": True,
        "notify_mail_to": "",  # empty: every admin with an email address
    },
    "nightly": {
        # Phase 12: every night the checks run for every customer; in the morning one summary with
        # proposals that are approved with one click (see services/nightly.py). Times are local
        # (schedules.timezone).
        "enabled": True,
        "run_at": "02:30",
        "summary_at": "07:00",
        "notify_app": True,
        "notify_mail": True,
        "mail_to": "",  # empty: every admin with an email address
        "checks": {"updates": True, "cves": True, "disks": True, "certificates": True, "backups": True,
                   "other": True},
        "cert_warn_days": 21,
        "cve_lookup": True,  # look up the CVE numbers that pending security updates fix (OSV.dev)
        "proposals": True,  # turn findings into approval requests that run with one click
        "use_window": True,  # approved changes wait for the customer's maintenance window
        "mail_when_clean": True,  # also mail when nothing needs attention
    },
    "remediation": {
        # Phase 14: automatic recovery of known problems with runbooks (see services/remediation.py).
        "enabled": True,
        "paused": False,  # the emergency stop: no new recoveries, running ones are cancelled
        "paused_by": None,
        "paused_at": None,
        "pause_note": "",
        "resumed_at": None,
        "max_runs_per_hour": 20,  # for the whole app
        "max_failures_per_hour": 5,  # more failed recoveries in an hour pause everything (circuit breaker)
        "service_check_minutes": 5,  # how often watched services are checked
        "verify_minutes": 15,  # after a run, the problem must be gone within this time to count as fixed
        "notify_app": True,
        "notify_mail": "failure",  # always | failure | never
        "mail_to": "",  # empty: every admin with an email address
    },
    "netwatch": {
        # Phase 15: network adapters that automatic recovery watches (see services/netwatch.py).
        "seen_up": {},  # {node id: {adapter: last time it was up}}
        "applied": {},  # {node id: hash of the watchdog configuration the agent has}
        "unsupported": [],  # machines with a local-fallback rule whose agent is too old
    },
    "proxy": {
        "mode": "direct",  # direct | proxy
        "trusted_proxies": [],
        "server_name": "_",
        "http_port": 80,
        "https_port": 443,
    },
}


# Names the product had before it was called Stackpilot. Settings saved under an older version store
# the full section, so a stored default name is shown as the current name instead.
LEGACY_NAMES = {"MCP Management Node", "Proxmox Management"}
_NAME_FIELDS = {"general": "app_name", "mail": "from_name"}


def _rename_legacy(key: str, value: dict[str, Any]) -> dict[str, Any]:
    field = _NAME_FIELDS.get(key)
    if field and value.get(field) in LEGACY_NAMES:
        value[field] = DEFAULTS[key][field]
    return value


APPLIED_PROXY_FILE = Path("/etc/mcp-management-node/proxy.json")


def _defaults(key: str) -> dict[str, Any]:
    value = dict(DEFAULTS.get(key, {}))
    if key == "updates":
        # A fresh install follows the channel of the build it was installed from.
        value["channel"] = get_build_info().channel
    elif key == "proxy":
        # Before anything is saved in the app, show what the installer configured.
        try:
            applied = json.loads(APPLIED_PROXY_FILE.read_text())
            value.update({k: v for k, v in applied.items() if k != "base_path"})
        except (OSError, ValueError):
            pass
    return value


async def get_setting(db: AsyncSession, key: str) -> dict[str, Any]:
    row = await db.get(Setting, key)
    value = _defaults(key)
    if row is not None:
        value.update(row.value or {})
    return _rename_legacy(key, value)


async def set_setting(db: AsyncSession, key: str, value: dict[str, Any]) -> dict[str, Any]:
    row = await db.get(Setting, key)
    merged = _defaults(key)
    if row is not None:
        merged.update(row.value or {})
        _rename_legacy(key, merged)
    merged.update(value)
    if row is None:
        db.add(Setting(key=key, value=merged))
    else:
        row.value = merged
    await db.flush()
    return merged
