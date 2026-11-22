"""Central container template library.

* **Official** templates come from the Proxmox VE appliance index (``pveam``). An entry follows
  the newest version of its family (e.g. ``debian-13-standard``) unless that is turned off.
* **Custom** templates are uploaded to the app (or fetched from a URL) and stored in the data
  directory, so they are part of the app's backups. Nodes download them over the agent's mTLS
  connection and check the SHA-256.

Per node the app scans the storages that hold ``vztmpl`` content; a template is *current* when
the library's file is there, *outdated* when an older file of the same template is there, and
*missing* otherwise. The nightly sync (a scheduled task) downloads what is missing or outdated.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mmn.config import get_settings
from mmn.db import sessionmaker
from mmn.models import CtTemplate, Node, TaskRun
from mmn.services import agent_hub, events, nodes
from mmn.services.agent_hub import AgentError
from mmn.services.nodes import NodeError
from mmn.services.settings_store import get_setting, set_setting

log = logging.getLogger("mmn.templates")

FNAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.+-]{0,200}\.tar\.(gz|xz|zst)$")


def templates_dir() -> Path:
    return get_settings().files_dir / "templates"


def parse_official(filename: str) -> tuple[str, str | None]:
    """``debian-13-standard_13.1-2_amd64.tar.zst`` -> (``debian-13-standard``, ``13.1-2``)."""
    base = re.sub(r"\.tar\.(gz|xz|zst)$", "", filename)
    parts = base.split("_")
    return parts[0], (parts[1] if len(parts) > 1 else None)


def custom_family(filename: str) -> str:
    base = re.sub(r"\.tar\.(gz|xz|zst)$", "", filename)
    return re.sub(r"[_-]v?\d[\w.+-]*$", "", base) or base


# ------------------------------------------------------------------ library
async def get_template(db: AsyncSession, tpl_id: str) -> CtTemplate:
    t = await db.get(CtTemplate, tpl_id)
    if t is None:
        t = (await db.execute(select(CtTemplate).where((CtTemplate.name == tpl_id) | (CtTemplate.filename == tpl_id)
                                                       | (CtTemplate.family == tpl_id)))).scalars().first()
    if t is None:
        raise NodeError("template not found")
    return t


async def list_templates(db: AsyncSession) -> list[CtTemplate]:
    return list((await db.execute(select(CtTemplate).order_by(CtTemplate.kind, CtTemplate.name))).scalars())


async def settings(db: AsyncSession) -> dict[str, Any]:
    return await get_setting(db, "templates")


def node_vztmpl_storages(node: Node) -> dict[str, dict[str, Any]]:
    return ((node.storage_scan or {}).get("storages")) or {}


def target_storage_name(tpl: CtTemplate, node: Node, default: str = "local") -> str:
    st = node_vztmpl_storages(node)
    want = tpl.storage or default
    if want in st or not st:
        return want
    return sorted(st)[0]


def _files(node: Node) -> dict[str, str]:
    """filename -> volid of every template file on the node."""
    out = {}
    for st in node_vztmpl_storages(node).values():
        for f in st.get("files") or []:
            out[f["filename"]] = f["volid"]
    return out


def volid_on_node(tpl: CtTemplate, node: Node) -> str | None:
    return _files(node).get(tpl.filename)


def status_on_node(tpl: CtTemplate, node: Node) -> dict[str, Any]:
    scan = node.storage_scan or {}
    if not scan.get("checked_at"):
        return {"status": "unknown"}
    files = _files(node)
    if tpl.filename in files:
        return {"status": "current", "volid": files[tpl.filename]}
    older = [f for f in files if f in (tpl.previous_filenames or [])
             or (tpl.kind == "official" and parse_official(f)[0] == tpl.family)
             or (tpl.kind == "custom" and custom_family(f) == tpl.family)]
    if older:
        return {"status": "outdated", "volid": files[older[0]], "present": sorted(older)}
    return {"status": "missing"}


def template_out(t: CtTemplate, node_list: list[Node] | None = None) -> dict[str, Any]:
    d = {"id": t.id, "name": t.name, "kind": t.kind, "family": t.family, "filename": t.filename,
         "version": t.version, "os": t.os, "description": t.description, "sha256": t.sha256, "size": t.size,
         "source_url": t.source_url, "sync": t.sync, "storage": t.storage, "follow_latest": t.follow_latest,
         "created_by": t.created_by, "created_at": t.created_at, "updated_at": t.updated_at}
    if node_list is not None:
        locs = []
        for n in node_list:
            st = status_on_node(t, n)
            locs.append({"node_id": n.id, "node": n.name, "environment": n.environment.name if n.environment else None,
                         "online": agent_hub.is_online(n.id), "storage": target_storage_name(t, n), **st})
        d["locations"] = locs
        d["summary"] = {k: sum(1 for loc in locs if loc["status"] == k)
                        for k in ("current", "outdated", "missing", "unknown")}
    return d


async def pve_nodes(db: AsyncSession) -> list[Node]:
    return [n for n in await nodes.list_nodes(db) if "guests" in (n.capabilities or [])]


# ------------------------------------------------------------------ official catalog
async def catalog(db: AsyncSession) -> dict[str, Any]:
    c = await get_setting(db, "template_catalog")
    return {"checked_at": c.get("checked_at"), "items": c.get("items") or []}


async def refresh_catalog(update_index: bool = True) -> dict[str, Any]:
    """Read the appliance index from an online node (after ``pveam update``)."""
    async with sessionmaker()() as db:
        cands = [n for n in await pve_nodes(db) if agent_hub.is_online(n.id)]
    if not cands:
        raise NodeError("no Proxmox VE node is online to read the template index from")
    node = cands[0]
    if update_index:
        try:
            await agent_hub.request(node.id, "exec", {"command": "pveam update", "timeout": 120}, timeout=150)
        except AgentError as e:
            log.info("pveam update on %s: %s", node.name, e)
    from mmn.services.guests import api

    raw = await api(node.id, "get", "/nodes/localhost/aplinfo", timeout=60) or []
    items = []
    for a in raw:
        fname = a.get("template") or ""
        if not FNAME_RE.match(fname):
            continue
        fam, ver = parse_official(fname)
        items.append({"filename": fname, "family": a.get("package") or fam, "version": a.get("version") or ver,
                      "os": a.get("os"), "section": a.get("section"), "headline": a.get("headline"),
                      "description": (a.get("description") or "")[:1000], "infopage": a.get("infopage")})
    items.sort(key=lambda i: (i["section"] != "system", i["family"]))
    async with sessionmaker()() as db:
        await set_setting(db, "template_catalog", {"checked_at": datetime.now(UTC).isoformat(), "items": items})
        # Follow the newest version of every official template.
        latest = {i["family"]: i for i in items}
        changed = []
        for t in await list_templates(db):
            if t.kind != "official" or not t.follow_latest:
                continue
            new = latest.get(t.family)
            if new and new["filename"] != t.filename:
                t.previous_filenames = [*[f for f in (t.previous_filenames or []) if f != new["filename"]],
                                        t.filename][-10:]
                t.filename, t.version, t.updated_at = new["filename"], new["version"], datetime.now(UTC)
                changed.append(t.name)
        await db.commit()
    return {"items": len(items), "updated": changed}


async def add_official(db: AsyncSession, filename: str, *, created_by: str | None, sync: bool = True,
                       storage: str | None = None) -> CtTemplate:
    if not FNAME_RE.match(filename):
        raise NodeError("invalid template file name")
    cat = await catalog(db)
    item = next((i for i in cat["items"] if i["filename"] == filename), None)
    fam, ver = parse_official(filename)
    if item is None and cat["items"]:
        raise NodeError("this template is not in the Proxmox VE index (refresh the list)")
    exists = (await db.execute(select(CtTemplate).where(CtTemplate.kind == "official",
                                                        CtTemplate.family == (item or {}).get("family", fam)))).scalars().first()
    if exists:
        raise NodeError("this template is already in the library")
    t = CtTemplate(name=(item or {}).get("headline") or fam, kind="official", family=(item or {}).get("family", fam),
                   filename=filename, version=(item or {}).get("version", ver), os=(item or {}).get("os"),
                   description=(item or {}).get("description"), sync=sync, storage=storage, follow_latest=True,
                   previous_filenames=[], created_by=created_by)
    db.add(t)
    await db.flush()
    return t


async def store_custom_file(src: Path, filename: str, *, name: str, description: str | None, created_by: str | None,
                            source_url: str | None = None, replace_id: str | None = None) -> CtTemplate:
    """Move an uploaded/downloaded file into the library. ``replace_id`` makes it the new
    version of an existing custom template (nodes with the old file become outdated)."""
    if not FNAME_RE.match(filename):
        raise NodeError("the file name must end in .tar.gz, .tar.xz or .tar.zst")
    h = hashlib.sha256()
    size = 0
    with src.open("rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
            size += len(chunk)
    if size < 1024:
        raise NodeError("the file is too small to be a container template")
    async with sessionmaker()() as db:
        if replace_id:
            t = await get_template(db, replace_id)
            if t.kind != "custom":
                raise NodeError("only custom templates can get a new file")
            if t.filename == filename:
                raise NodeError("give the new version another file name (e.g. with a version number)")
            t.previous_filenames = [*(t.previous_filenames or []), t.filename][-10:]
            old_dir = templates_dir() / t.id
            t.filename, t.sha256, t.size, t.updated_at = filename, h.hexdigest(), size, datetime.now(UTC)
            if source_url:
                t.source_url = source_url
        else:
            t = CtTemplate(name=name, kind="custom", family=custom_family(filename), filename=filename,
                           description=description, sha256=h.hexdigest(), size=size, source_url=source_url,
                           previous_filenames=[], sync=True, follow_latest=False, created_by=created_by)
            db.add(t)
            await db.flush()
            old_dir = None
        dest_dir = templates_dir() / t.id
        dest_dir.mkdir(parents=True, exist_ok=True)
        if old_dir is not None:
            for p in old_dir.iterdir():
                if p.is_file():
                    p.unlink()
        shutil.move(str(src), dest_dir / filename)
        await db.commit()
        await db.refresh(t)
        return t


def custom_file(t: CtTemplate) -> Path | None:
    p = templates_dir() / t.id / t.filename
    return p if p.is_file() else None


async def download_custom(url: str, filename: str, *, name: str, description: str | None,
                          created_by: str | None, replace_id: str | None = None) -> None:
    """Fetch a custom template from a URL in the background (progress as events)."""
    import httpx

    tmp_dir = templates_dir() / "_incoming"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    tmp = tmp_dir / f"{hashlib.sha1(url.encode()).hexdigest()}.part"
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=httpx.Timeout(60, read=300)) as c:
            async with c.stream("GET", url) as r:
                r.raise_for_status()
                total = int(r.headers.get("content-length") or 0)
                done, last = 0, 0
                with tmp.open("wb") as f:
                    async for chunk in r.aiter_bytes(1 << 20):
                        f.write(chunk)
                        done += len(chunk)
                        if done - last > 32 << 20:
                            last = done
                            events.publish("template.download", {"url": url, "done": done, "total": total})
        t = await store_custom_file(tmp, filename, name=name, description=description, created_by=created_by,
                                    source_url=url, replace_id=replace_id)
        events.publish("template.download", {"url": url, "finished": True, "template_id": t.id})
    except Exception as e:  # noqa: BLE001
        log.warning("template download %s failed: %s", url, e)
        events.publish("template.download", {"url": url, "error": str(e) or type(e).__name__})
    finally:
        tmp.unlink(missing_ok=True)


async def delete_template(db: AsyncSession, t: CtTemplate) -> None:
    d = templates_dir() / t.id
    if d.exists():
        shutil.rmtree(d, ignore_errors=True)
    await db.delete(t)
    await db.flush()


# ------------------------------------------------------------------ nodes
async def scan_node(node_id: str) -> dict[str, Any]:
    """Read which template files the node's vztmpl storages hold."""
    from mmn.services.guests import api

    storages = await api(node_id, "get", "/nodes/localhost/storage", {"content": "vztmpl", "enabled": True}) or []
    out: dict[str, Any] = {}
    for s in storages:
        name = s.get("storage")
        if not name or not s.get("active", 1):
            continue
        try:
            content = await api(node_id, "get", f"/nodes/localhost/storage/{name}/content", {"content": "vztmpl"}) or []
        except AgentError:
            continue
        files = []
        for c in content:
            volid = c.get("volid") or ""
            fname = volid.split("/", 1)[-1]
            files.append({"volid": volid, "filename": fname, "size": c.get("size"), "ctime": c.get("ctime")})
        out[name] = {"shared": bool(s.get("shared")), "type": s.get("type"), "files": files}
    scan = {"checked_at": datetime.now(UTC).isoformat(), "storages": out}
    async with sessionmaker()() as db:
        n = await db.get(Node, node_id)
        if n is not None:
            n.storage_scan = scan
            await db.commit()
    return scan


async def scan_all() -> None:
    async with sessionmaker()() as db:
        ids = [n.id for n in await pve_nodes(db) if agent_hub.is_online(n.id)]
    for nid in ids:
        try:
            await scan_node(nid)
        except AgentError as e:
            log.info("template scan on %s: %s", nid, e)


async def push_to_node(tpl_id: str, node_id: str, *, parent: str | None = None, source: str = "ui",
                       requested_by: str | None = None) -> tuple[str, dict[str, Any], str | None]:
    """Download one library template to a node and wait for it."""
    async with sessionmaker()() as db:
        t = await get_template(db, tpl_id)
        node = await db.get(Node, node_id)
        default = (await settings(db)).get("storage") or "local"
        storage = target_storage_name(t, node, default)
    label = {"template": t.filename, "storage": storage}
    if t.kind == "official":
        job = await nodes.start_job(node_id, "template.download", "pve.task",
                                    {"method": "create", "path": "/nodes/localhost/aplinfo",
                                     "args": {"storage": storage, "template": t.filename}},
                                    source=source, requested_by=requested_by, exclusive=False, parent=parent,
                                    label=label)
    else:
        if custom_file(t) is None:
            raise NodeError("the template file is missing from the app's data directory")
        job = await nodes.start_job(node_id, "template.download", "file.fetch",
                                    {"url": f"/agent/templates/{t.id}/{t.filename}", "sha256": t.sha256,
                                     "volid": f"{storage}:vztmpl/{t.filename}", "size": t.size or 0},
                                    source=source, requested_by=requested_by, exclusive=False, parent=parent,
                                    label=label)
    return await agent_hub.wait_job(job.id, 3 * 3600)


async def sync(*, template_ids: list[str] | None = None, node_ids: list[str] | None = None,
               environment_ids: list[str] | None = None, trigger: str = "schedule", requested_by: str | None = None,
               task_id: str | None = None, name: str = "Template sync", refresh_index: bool = True) -> str:
    """Bring every (selected) template to every (selected) online node. Returns the TaskRun id;
    the work continues in the background."""
    async with sessionmaker()() as db:
        run = TaskRun(task_id=task_id, name=name, kind="template_sync", trigger=trigger, requested_by=requested_by,
                      status="running", results=[])
        db.add(run)
        await db.commit()
    agent_hub.spawn(_sync(run.id, template_ids, node_ids, environment_ids, requested_by, refresh_index))
    return run.id


async def _sync(run_id: str, template_ids, node_ids, environment_ids, requested_by, refresh_index) -> list[dict]:
    from mmn.services import tasks

    results: list[dict[str, Any]] = []
    try:
        if refresh_index:
            try:
                await refresh_catalog()
            except (NodeError, AgentError) as e:
                results.append({"target": "template index", "status": "failed", "error": str(e)})
        await scan_all()
        async with sessionmaker()() as db:
            tpls = [t for t in await list_templates(db) if (t.id in template_ids if template_ids else t.sync)]
            all_nodes = [n for n in await pve_nodes(db)
                         if (not node_ids or n.id in node_ids)
                         and (not environment_ids or n.environment_id in environment_ids)]
            default = (await settings(db)).get("storage") or "local"
            tpl_ids = [t.id for t in tpls]

        async def per_node(n: Node) -> None:
            if not agent_hub.is_online(n.id):
                for t in tpls:
                    results.append({"target": f"{t.name} → {n.name}", "node_id": n.id, "template_id": t.id,
                                    "status": "skipped", "error": "node offline"})
                return
            for tid in tpl_ids:
                async with sessionmaker()() as db:
                    t = await db.get(CtTemplate, tid)
                    node = await db.get(Node, n.id)
                    st = status_on_node(t, node)
                    storage = target_storage_name(t, node, default)
                    shared = node_vztmpl_storages(node).get(storage, {}).get("shared")
                row = {"target": f"{t.name} → {n.name}", "node_id": n.id, "template_id": t.id, "storage": storage}
                if st["status"] == "current":
                    results.append({**row, "status": "success", "note": "already current"})
                    continue
                if shared and any(r.get("template_id") == t.id and r.get("storage") == storage
                                  and r.get("cluster") == n.cluster_name and r["status"] == "success"
                                  for r in results if r.get("cluster")):
                    results.append({**row, "status": "success", "note": "shared storage, done via another node"})
                    continue
                try:
                    status, _, err = await push_to_node(t.id, n.id, source="system", requested_by=requested_by)
                except (NodeError, AgentError) as e:
                    status, err = "failed", str(e)
                results.append({**row, "cluster": n.cluster_name, "status": "success" if status == "success" else "failed",
                                "error": err})
            try:
                await scan_node(n.id)
            except AgentError:
                pass

        await asyncio.gather(*(per_node(n) for n in all_nodes))
    except Exception as e:  # noqa: BLE001
        log.exception("template sync failed")
        results.append({"target": "sync", "status": "failed", "error": f"{type(e).__name__}: {e}"})
    await tasks.finish_run(run_id, results)
    return results
