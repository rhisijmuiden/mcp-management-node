"""Community scripts (community-scripts.org, repository community-scripts/ProxmoxVE).

The list is synced from the official repository (nightly and on request): every ``ct/*.sh`` and
``vm/*.sh`` script, with the defaults each script declares (``var_cpu``, ``var_ram``, ``var_disk``,
``var_os``, ``var_version``, ``var_tags``). Before a script runs, the app fetches it again from the
official repository and shows it; the run uses exactly that text (the UI sends back its SHA-256,
so a script that changed in between has to be reviewed again).

Scripts run as root on the chosen Proxmox VE node. "Default" runs unattended (``mode=default``
plus the settings from the dialog) as a job with a streamed log; "Advanced" opens the script's
own wizard in a terminal in the browser.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
from datetime import UTC, datetime
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mmn.db import sessionmaker
from mmn.models import CommunityScript, Node
from mmn.services import agent_hub, nodes
from mmn.services.nodes import NodeError
from mmn.services.settings_store import get_setting, set_setting

log = logging.getLogger("mmn.community")

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,99}$")
VAR_RE = re.compile(r'^var_([a-z_]+)="\$\{var_[a-z_]+:-([^}"]*)\}"', re.M)
APP_RE = re.compile(r'^APP="([^"]{1,200})"', re.M)
SOURCE_RE = re.compile(r"^#\s*Source:\s*(\S+)", re.M)
UA = {"User-Agent": "mcp-management-node", "Accept": "application/vnd.github+json"}


async def config(db: AsyncSession) -> dict[str, Any]:
    return await get_setting(db, "community")


def raw_url(cfg: dict[str, Any], path: str) -> str:
    return f"{cfg['raw_base'].rstrip('/')}/{path}"


def parse_script(text: str, slug: str) -> dict[str, Any]:
    vars_ = {k: v for k, v in VAR_RE.findall(text)}
    app = APP_RE.search(text)
    src = SOURCE_RE.search(text)

    def num(k: str) -> int | None:
        try:
            return int(float(vars_[k]))
        except (KeyError, ValueError):
            return None

    tags = [t for t in re.split(r"[;, ]+", vars_.get("tags", "")) if t]
    return {
        "name": app.group(1) if app else slug,
        "source_url": src.group(1) if src and src.group(1).startswith("http") else None,
        "tags": tags[:20],
        "defaults": {"cpu": num("cpu"), "ram": num("ram"), "disk": num("disk"), "os": vars_.get("os"),
                     "version": vars_.get("version"), "unprivileged": vars_.get("unprivileged", "1") != "0"},
    }


def script_out(s: CommunityScript) -> dict[str, Any]:
    return {"slug": s.slug, "kind": s.kind, "name": s.name, "path": s.path, "source_url": s.source_url,
            "tags": s.tags or [], "defaults": s.defaults or {}, "updated_at": s.updated_at,
            "page_url": f"https://community-scripts.org/scripts/{s.slug}"}


async def list_scripts(db: AsyncSession, query: str | None = None, kind: str | None = None) -> list[dict[str, Any]]:
    rows = (await db.execute(select(CommunityScript).order_by(CommunityScript.name))).scalars().all()
    out = []
    for s in rows:
        if kind and s.kind != kind:
            continue
        if query:
            q = query.lower()
            if q not in s.name.lower() and q not in s.slug and not any(q in t for t in s.tags or []):
                continue
        out.append(script_out(s))
    return out


async def sync() -> dict[str, Any]:
    """Refresh the list from the official repository. Only scripts whose file changed are read."""
    async with sessionmaker()() as db:
        cfg = await config(db)
        known = {s.slug: s for s in (await db.execute(select(CommunityScript))).scalars()}
    headers = dict(UA)
    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as c:
        r = await c.get(f"https://api.github.com/repos/{cfg['repo']}/git/trees/{cfg['branch']}",
                        params={"recursive": "1"}, headers=headers)
        r.raise_for_status()
        tree = r.json().get("tree") or []
        wanted: dict[str, tuple[str, str, str]] = {}
        for e in tree:
            path = e.get("path") or ""
            m = re.match(r"^(ct|vm)/([a-z0-9][a-z0-9._-]*)\.sh$", path)
            if e.get("type") == "blob" and m and SLUG_RE.match(m.group(2)):
                name = m.group(2)
                slug = name if m.group(1) == "ct" or name.endswith("-vm") else f"{name}-vm"
                wanted[slug] = (m.group(1), path, e.get("sha") or "")
        changed = [s for s, (_, _, sha) in wanted.items() if s not in known or known[s].blob_sha != sha]
        sem = asyncio.Semaphore(8)
        parsed: dict[str, dict[str, Any]] = {}

        async def fetch(slug: str) -> None:
            kind, path, _ = wanted[slug]
            async with sem:
                try:
                    rr = await c.get(raw_url(cfg, path), headers={"User-Agent": UA["User-Agent"]})
                    rr.raise_for_status()
                    parsed[slug] = parse_script(rr.text, slug)
                except httpx.HTTPError as e:
                    log.info("community script %s: %s", path, e)

        await asyncio.gather(*(fetch(s) for s in changed))
    now = datetime.now(UTC)
    async with sessionmaker()() as db:
        for slug, (kind, path, sha) in wanted.items():
            if slug not in parsed:
                continue
            p = parsed[slug]
            row = await db.get(CommunityScript, slug)
            if row is None:
                row = CommunityScript(slug=slug, kind=kind, path=path)
                db.add(row)
            row.kind, row.path, row.blob_sha, row.updated_at = kind, path, sha, now
            row.name, row.source_url, row.tags, row.defaults = p["name"], p["source_url"], p["tags"], p["defaults"]
        removed = [s for s in known if s not in wanted]
        for slug in removed:
            row = await db.get(CommunityScript, slug)
            if row is not None:
                await db.delete(row)
        await set_setting(db, "community_state", {"synced_at": now.isoformat(), "count": len(wanted),
                                                  "error": None})
        await db.commit()
    return {"count": len(wanted), "updated": len(parsed), "removed": len(removed)}


async def sync_safe() -> None:
    try:
        await sync()
    except Exception as e:  # noqa: BLE001
        log.warning("community script sync failed: %s", e)
        async with sessionmaker()() as db:
            await set_setting(db, "community_state", {"error": f"{type(e).__name__}: {e}"})
            await db.commit()


_cache: dict[str, tuple[float, dict[str, Any]]] = {}


async def fetch_script(db: AsyncSession, slug: str) -> dict[str, Any]:
    """The script text as it is now in the official repository, plus the install part it loads."""
    s = await db.get(CommunityScript, slug)
    if s is None:
        raise NodeError("script not found (sync the list first)")
    hit = _cache.get(slug)
    if hit and time.monotonic() - hit[0] < 120:
        return hit[1]
    cfg = await config(db)
    async with httpx.AsyncClient(timeout=30, follow_redirects=True, headers={"User-Agent": UA["User-Agent"]}) as c:
        r = await c.get(raw_url(cfg, s.path))
        r.raise_for_status()
        text = r.text
        install = None
        if s.kind == "ct":
            base = s.path.split("/", 1)[1].removesuffix(".sh")
            ri = await c.get(raw_url(cfg, f"install/{base}-install.sh"))
            if ri.status_code == 200:
                install = ri.text
    out = {**script_out(s), "script": text, "sha256": hashlib.sha256(text.encode()).hexdigest(),
           "script_url": raw_url(cfg, s.path), "install_script": install,
           "install_url": raw_url(cfg, f"install/{s.path.split('/', 1)[1].removesuffix('.sh')}-install.sh")
           if s.kind == "ct" else None,
           "command": f'bash -c "$(curl -fsSL {raw_url(cfg, s.path)})"'}
    _cache[slug] = (time.monotonic(), out)
    return out


def install_env(opts: dict[str, Any], kind: str) -> dict[str, str]:
    """Translate the dialog's settings into the variables the community-scripts engine reads."""
    env: dict[str, str] = {}
    m = {"cpu": "var_cpu", "ram": "var_ram", "disk": "var_disk", "hostname": "var_hostname", "vmid": "var_ctid",
         "bridge": "var_brg", "vlan": "var_vlan", "storage": "var_container_storage",
         "template_storage": "var_template_storage", "ssh_key": "var_ssh_authorized_key", "tags": "var_tags",
         "gateway": "var_gateway", "password": "var_pw"}
    for k, var in m.items():
        v = opts.get(k)
        if v in (None, "", []):
            continue
        env[var] = ";".join(v) if isinstance(v, list) else str(v)
    if opts.get("ip") and opts["ip"] != "dhcp":
        env["var_net"] = str(opts["ip"])
    elif opts.get("ip") == "dhcp":
        env["var_net"] = "dhcp"
    if opts.get("unprivileged") is not None:
        env["var_unprivileged"] = "1" if opts["unprivileged"] else "0"
    if opts.get("ssh_key"):
        env["var_ssh"] = "yes"
    if kind == "vm" and "var_ctid" in env:
        env["var_vmid"] = env.pop("var_ctid")
    return env


async def start_install(db: AsyncSession, slug: str, node_id: str, sha256: str, opts: dict[str, Any], *,
                        source: str = "ui", requested_by: str | None = None):
    """Unattended run (mode=default) as a job. Returns the job."""
    node = await nodes.get_node(db, node_id)
    if "guests" not in (node.capabilities or []):
        raise NodeError("community scripts run on Proxmox VE nodes only")
    agent_hub.get(node.id)
    info = await fetch_script(db, slug)
    if sha256 != info["sha256"]:
        raise NodeError("the script changed since it was shown: review it again")
    env = {**install_env(opts, info["kind"]), "mode": "default", "MODE": "default", "PHS_SILENT": "1"}
    from mmn.services.guests import script

    return await script(node.id, "community.install", info["script"], env=env, timeout=3 * 3600, source=source,
                        requested_by=requested_by, label={"slug": slug, "script_sha256": sha256})


async def interactive_env(db: AsyncSession, slug: str, sha256: str, opts: dict[str, Any]) -> tuple[str, dict[str, str]]:
    info = await fetch_script(db, slug)
    if sha256 != info["sha256"]:
        raise NodeError("the script changed since it was shown: review it again")
    return info["script"], install_env(opts, info["kind"])


async def state(db: AsyncSession) -> dict[str, Any]:
    st = await get_setting(db, "community_state")
    cfg = await config(db)
    return {**st, "repo": cfg["repo"], "branch": cfg["branch"]}


async def node_ok(db: AsyncSession, node_id: str) -> Node:
    node = await nodes.get_node(db, node_id)
    if "guests" not in (node.capabilities or []):
        raise NodeError("community scripts run on Proxmox VE nodes only")
    return node
