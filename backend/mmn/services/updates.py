"""Update checks against GitHub releases and handing update requests to the privileged updater.

Releases
--------
CI publishes a GitHub release for every push:
* ``main``  -> ``vX.Y.Z`` (production channel)
* ``test``  -> ``vX.Y.Z-test.N`` (prerelease, test channel)
Each release has the asset ``mcp-management-node-<tag>.tar.gz`` and ``...tar.gz.sha256``.

The app itself runs unprivileged. To install, it writes ``run/update-request.json``; a systemd
path unit starts ``mcp-management-node-updater.service`` (root), which re-fetches the release
from the repository configured in ``/etc/mcp-management-node/mmn.env``, verifies the checksum,
migrates the database and restarts the service. Status is reported in ``run/update-status.json``.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from mmn.config import get_settings
from mmn.services.settings_store import get_setting, set_setting
from mmn.version import channel_of_tag, get_build_info, parse_tag

ASSET_PREFIX = "mcp-management-node-"
CHANNELS = ("production", "test")


class UpdateError(Exception):
    pass


def _headers() -> dict[str, str]:
    h = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
         "User-Agent": "mcp-management-node-updater"}
    token = get_settings().github_token
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


async def fetch_releases() -> list[dict[str, Any]]:
    repo = get_settings().github_repo
    async with httpx.AsyncClient(timeout=20, headers=_headers()) as client:
        r = await client.get(f"https://api.github.com/repos/{repo}/releases", params={"per_page": 50})
    if r.status_code == 404:
        raise UpdateError("repository not found or private: set MMN_GITHUB_TOKEN")
    if r.status_code >= 400:
        raise UpdateError(f"GitHub answered {r.status_code}")
    out = []
    for rel in r.json():
        tag = rel.get("tag_name", "")
        ver = parse_tag(tag)
        if rel.get("draft") or ver is None:
            continue
        asset = next((a for a in rel.get("assets", []) if a["name"] == f"{ASSET_PREFIX}{tag}.tar.gz"), None)
        if asset is None:
            continue
        out.append({
            "tag": tag, "version": tag.lstrip("v"), "channel": channel_of_tag(tag),
            "name": rel.get("name") or tag, "notes": (rel.get("body") or "")[:20000],
            "published_at": rel.get("published_at"), "url": rel.get("html_url"), "_ver": ver,
        })
    return out


def latest_for_channel(releases: list[dict[str, Any]], channel: str) -> dict[str, Any] | None:
    # The test channel follows the newest build of either channel, production only stable ones.
    pool = [r for r in releases if channel == "test" or r["channel"] == "production"]
    return max(pool, key=lambda r: r["_ver"], default=None)


async def check_for_updates(db: AsyncSession) -> dict[str, Any]:
    cfg = await get_setting(db, "updates")
    build = get_build_info()
    releases = await fetch_releases()
    latest = latest_for_channel(releases, cfg["channel"])
    current = parse_tag(build.tag)
    result = {
        "checked_at": datetime.now(UTC).isoformat(),
        "channel": cfg["channel"],
        "current": build.tag,
        "latest": {k: v for k, v in latest.items() if not k.startswith("_")} if latest else None,
        "update_available": bool(latest and current and latest["_ver"] > current) or bool(
            latest and build.channel != cfg["channel"] and latest["tag"] != build.tag),
        "is_downgrade": bool(latest and current and latest["_ver"] < current),
    }
    await set_setting(db, "updates", {"last_check": result["checked_at"], "latest": result["latest"],
                                      "update_available": result["update_available"]})
    return result


def updater_available() -> bool:
    s = get_settings()
    return (s.install_dir / "current").exists() and Path("/run/systemd/system").exists()


def status_file() -> Path:
    return get_settings().run_dir / "update-status.json"


def request_file() -> Path:
    return get_settings().run_dir / "update-request.json"


def read_status() -> dict[str, Any]:
    f = status_file()
    if not f.exists():
        return {"state": "idle"}
    try:
        return json.loads(f.read_text())
    except (OSError, ValueError):
        return {"state": "unknown"}


def request_update(tag: str, channel: str, requested_by: str) -> None:
    if parse_tag(tag) is None:
        raise UpdateError("invalid tag")
    if channel not in CHANNELS:
        raise UpdateError("invalid channel")
    if not updater_available():
        raise UpdateError("the updater is only available on an installed system")
    st = read_status()
    if st.get("state") in ("queued", "running"):
        raise UpdateError("an update is already running")
    s = get_settings()
    s.run_dir.mkdir(parents=True, exist_ok=True)
    status_file().write_text(json.dumps({"state": "queued", "tag": tag, "channel": channel,
                                         "at": datetime.now(UTC).isoformat(), "steps": []}))
    tmp = request_file().with_suffix(".tmp")
    tmp.write_text(json.dumps({"action": "update", "tag": tag, "channel": channel, "by": requested_by}))
    os.replace(tmp, request_file())  # the path unit starts the updater


async def set_channel(db: AsyncSession, channel: str) -> dict[str, Any]:
    if channel not in CHANNELS:
        raise UpdateError("invalid channel")
    return await set_setting(db, "updates", {"channel": channel})
