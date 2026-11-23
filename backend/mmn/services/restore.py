"""Restores from Proxmox VE backups (phase 11): a whole guest, or files from a PBS backup.

* Full guest: ``POST /nodes/<n>/qemu`` (``archive``) or ``/lxc`` (``ostemplate`` + ``restore``) as
  a Proxmox VE task, to a new VMID or over the existing guest (``force``; the guest must be
  stopped). The interface asks to type the VMID, the AI gateway asks approval by default.
* Files (Proxmox Backup Server only): browse a backup with the file-restore API and extract a file
  or folder with ``proxmox-file-restore`` on the node, into a folder on that node. From there
  ``copy_data`` moves it to the machine that needs it.
* A one-off backup of a guest ("back up now", vzdump) outside the jobs.
"""

from __future__ import annotations

import base64
import posixpath
import re
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from mmn.models import Node, NodeJob
from mmn.services import agent_hub, guests, nodes
from mmn.services.nodes import NodeError

VOLUME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,99}:[A-Za-z0-9_./+:-]{1,250}$")
TARGET_RE = re.compile(r"^/[A-Za-z0-9_./-]{1,200}$")
RESTORE_ROOT = "/var/lib/stackpilot-restore"
PBS_SNAP_RE = re.compile(r"^backup/(?:vm|ct|host)/[A-Za-z0-9_.-]+/\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


def check_volume(volume: str) -> tuple[str, str]:
    if not VOLUME_RE.match(volume or "") or ".." in volume:
        raise NodeError("invalid backup volume id (looks like storage:backup/vzdump-qemu-100-....vma.zst)")
    storage, rest = volume.split(":", 1)
    return storage, rest


def volume_type(volume: str) -> str:
    _, rest = check_volume(volume)
    if rest.startswith("backup/ct/") or "vzdump-lxc-" in rest or "vzdump-openvz-" in rest:
        return "lxc"
    if rest.startswith("backup/vm/") or "vzdump-qemu-" in rest:
        return "qemu"
    raise NodeError("cannot tell from the volume id whether this is a VM or a container backup")


def volume_vmid(volume: str) -> int | None:
    _, rest = check_volume(volume)
    m = re.match(r"^backup/(?:vm|ct)/(\d+)/", rest) or re.search(r"vzdump-(?:qemu|lxc|openvz)-(\d+)-", rest)
    return int(m.group(1)) if m else None


async def cluster_members(db: AsyncSession, node: Node) -> list[Node]:
    if not node.cluster_name:
        return [node]
    return [n for n in await nodes.list_nodes(db, node.environment_id)
            if n.kind == "proxmox" and n.cluster_name == node.cluster_name]


def _find_in_cluster(members: list[Node], vmid: int) -> tuple[Node, dict[str, Any]] | None:
    for m in members:
        for g in guests.guests_of(m):
            if g["vmid"] == vmid and (g.get("pve_node") in (None, m.pve_name, m.name)):
                return m, g
    for m in members:
        for g in guests.guests_of(m):
            if g["vmid"] == vmid:
                return m, g
    return None


async def guest_backups(db: AsyncSession, node: Node, vmid: int) -> list[dict[str, Any]]:
    from mmn.services import protection

    members = await cluster_members(db, node)
    anchor = node if agent_hub.is_online(node.id) else next((m for m in members if agent_hub.is_online(m.id)), None)
    if anchor is None:
        raise NodeError("no node of this cluster is online")
    return await protection.list_volumes(anchor, members, vmid=vmid)


class RestoreIn(BaseModel):
    volume: str = Field(max_length=300)
    vmid: int | None = Field(default=None, ge=100, le=999_999_999)  # None: a free VMID (or the original with overwrite)
    storage: str | None = Field(default=None, pattern=guests.STORAGE_RE.pattern)
    overwrite: bool = False
    start: bool = False
    unique: bool = False  # new MAC addresses (a copy next to the original)


async def restore_guest(db: AsyncSession, node: Node, body: RestoreIn, *, source: str = "ui",
                        requested_by: str | None = None) -> tuple[NodeJob, dict[str, Any]]:
    """Restore a backup as a guest on ``node``. Returns the job and what is restored where."""
    kind = volume_type(body.volume)
    members = await cluster_members(db, node)
    target_vmid = body.vmid or (volume_vmid(body.volume) if body.overwrite else None)
    existing = _find_in_cluster(members, target_vmid) if target_vmid else None
    run_on = node
    if existing:
        if not body.overwrite:
            raise NodeError(f"VMID {target_vmid} exists already; choose another VMID or overwrite it")
        on, g = existing
        if g["status"] != "stopped":
            raise NodeError(f"stop {g['name'] or target_vmid} first; a running guest cannot be overwritten")
        if g["type"] != kind:
            raise NodeError(f"VMID {target_vmid} is a {'container' if g['type'] == 'lxc' else 'VM'}, the backup is not")
        pve = g.get("pve_node") or on.pve_name
        run_on = next((m for m in members if (m.pve_name or m.name) == pve), on)
    elif body.overwrite and target_vmid is None:
        raise NodeError("give the VMID to overwrite")
    if not agent_hub.is_online(run_on.id):
        raise NodeError(f"the agent of {run_on.name} is not connected")
    if target_vmid is None:
        target_vmid = int(await guests.api(run_on.id, "get", "/cluster/nextid"))
    if kind == "lxc" and not body.storage and not existing:
        raise NodeError("choose the storage for the container's disks")
    args: dict[str, Any] = {"vmid": target_vmid}
    if kind == "qemu":
        args["archive"] = body.volume
    else:
        args.update({"ostemplate": body.volume, "restore": 1})
    if body.storage:
        args["storage"] = body.storage
    if existing:
        args["force"] = 1
    if body.unique:
        args["unique"] = 1
    if body.start:
        args["start"] = 1
    path = f"/nodes/localhost/{'qemu' if kind == 'qemu' else 'lxc'}"
    job = await guests.task(run_on.id, "backup.restore", "create", path, args, source=source, requested_by=requested_by,
                            vmid=target_vmid, label={"volume": body.volume, "overwrite": bool(existing)})
    return job, {"vmid": target_vmid, "type": kind, "node": run_on.name, "node_id": run_on.id,
                 "overwrite": bool(existing), "volume": body.volume}


# ------------------------------------------------------------------ file-level restore (PBS)
def _b64(path: str) -> str:
    return base64.b64encode(path.encode()).decode() if path not in ("", "/") else "/"


def _check_path(path: str) -> str:
    path = "/" + (path or "").lstrip("/")
    if ".." in path.split("/") or len(path) > 1000 or any(c in path for c in "\n\r\0"):
        raise NodeError("invalid path inside the backup")
    return path


async def _pbs_storage(node: Node, storage: str) -> dict[str, Any]:
    cfg = await guests.api(node.id, "get", f"/storage/{storage}", timeout=30) or {}
    if cfg.get("type") != "pbs":
        raise NodeError("file-level restore works for backups on a Proxmox Backup Server storage")
    return cfg


async def list_files(node: Node, volume: str, path: str = "/") -> dict[str, Any]:
    """Browse a PBS backup: entries of ``path`` (for a VM, the first levels are disks and partitions)."""
    storage, rest = check_volume(volume)
    path = _check_path(path)
    if not PBS_SNAP_RE.match(rest):
        raise NodeError("file-level restore works for backups on a Proxmox Backup Server storage")
    await _pbs_storage(node, storage)
    rows = await guests.api(node.id, "get", f"/nodes/localhost/storage/{storage}/file-restore/list",
                            {"volume": volume, "filepath": _b64(path)}, timeout=180) or []
    out = []
    for r in rows:
        try:
            p = base64.b64decode(r.get("filepath") or "").decode(errors="replace")
        except ValueError:
            p = str(r.get("filepath"))
        out.append({"path": p, "name": r.get("text") or posixpath.basename(p.rstrip("/")) or p,
                    "type": r.get("type"), "leaf": bool(r.get("leaf")), "size": r.get("size"),
                    "mtime": datetime.fromtimestamp(int(r["mtime"]), UTC).isoformat() if r.get("mtime") else None})
    out.sort(key=lambda e: (e["leaf"], e["name"].lower()))
    return {"volume": volume, "path": path, "entries": out}


FILE_RESTORE_SCRIPT = r"""set -eu
command -v proxmox-file-restore >/dev/null 2>&1 || { echo "proxmox-file-restore is not installed on this node (package proxmox-backup-file-restore)"; exit 2; }
cfg=$(pvesh get "/storage/$MMN_STORE" --output-format json)
val(){ printf '%s' "$cfg" | grep -o "\"$1\":\"[^\"]*\"" | head -1 | cut -d'"' -f4; }
num(){ printf '%s' "$cfg" | grep -o "\"$1\":[0-9]*" | head -1 | cut -d: -f2; }
server=$(val server); store=$(val datastore); user=$(val username); ns=$(val namespace); port=$(num port)
export PBS_FINGERPRINT="$(val fingerprint)"
PBS_PASSWORD="$(cat "/etc/pve/priv/storage/$MMN_STORE.pw")"; export PBS_PASSWORD
repo="$user@$server${port:+:$port}:$store"
mkdir -p "$MMN_TARGET_DIR"
out="$MMN_TARGET_DIR/$(basename "$MMN_PATH")"
echo "Extracting $MMN_PATH from $MMN_SNAPSHOT to $out"
if [ -n "$ns" ]; then
  proxmox-file-restore extract "$MMN_SNAPSHOT" "$MMN_PATH" "$out" --repository "$repo" --ns "$ns"
else
  proxmox-file-restore extract "$MMN_SNAPSHOT" "$MMN_PATH" "$out" --repository "$repo"
fi
du -sh "$out" 2>/dev/null || true
echo "Done: $out"
"""


class FileRestoreIn(BaseModel):
    volume: str = Field(max_length=300)
    path: str = Field(min_length=2, max_length=1000)
    target_dir: str | None = Field(default=None, pattern=TARGET_RE.pattern)


async def restore_files(node: Node, body: FileRestoreIn, *, source: str = "ui",
                        requested_by: str | None = None) -> tuple[NodeJob, str]:
    storage, rest = check_volume(body.volume)
    path = _check_path(body.path)
    if not PBS_SNAP_RE.match(rest):
        raise NodeError("file-level restore works for backups on a Proxmox Backup Server storage")
    await _pbs_storage(node, storage)
    target = body.target_dir or f"{RESTORE_ROOT}/{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}"
    if ".." in target or target.rstrip("/") in ("", "/etc", "/usr", "/boot", "/root", "/var", "/bin", "/sbin"):
        raise NodeError("choose an empty folder for the restored files, for example " + RESTORE_ROOT)
    env = {"MMN_STORE": storage, "MMN_SNAPSHOT": rest.removeprefix("backup/"), "MMN_PATH": path,
           "MMN_TARGET_DIR": target.rstrip("/")}
    job = await guests.script(node.id, "backup.file_restore", FILE_RESTORE_SCRIPT, env=env, timeout=3 * 3600,
                              source=source, requested_by=requested_by,
                              label={"volume": body.volume, "path": path, "target_dir": env["MMN_TARGET_DIR"]})
    return job, f"{env['MMN_TARGET_DIR']}/{posixpath.basename(path.rstrip('/'))}"


# ------------------------------------------------------------------ back up one guest now
class BackupNowIn(BaseModel):
    vmid: int = Field(ge=100, le=999_999_999)
    storage: str = Field(pattern=guests.STORAGE_RE.pattern)
    mode: Literal["snapshot", "suspend", "stop"] = "snapshot"
    compress: Literal["zstd", "lzo", "gzip", "0"] = "zstd"
    notes: str = Field(default="{{guestname}} (Stackpilot)", max_length=200)
    protected: bool = False


async def backup_now(db: AsyncSession, node: Node, body: BackupNowIn, *, source: str = "ui",
                     requested_by: str | None = None) -> NodeJob:
    members = await cluster_members(db, node)
    hit = _find_in_cluster(members, body.vmid)
    if hit is None:
        raise NodeError(f"guest {body.vmid} not found in this cluster")
    on, g = hit
    pve = g.get("pve_node") or on.pve_name
    run_on = next((m for m in members if (m.pve_name or m.name) == pve), on)
    if not agent_hub.is_online(run_on.id):
        raise NodeError(f"the agent of {run_on.name} is not connected")
    args: dict[str, Any] = {"vmid": str(body.vmid), "storage": body.storage, "mode": body.mode,
                            "compress": body.compress, "notes-template": body.notes}
    if body.protected:
        args["protected"] = 1
    return await guests.task(run_on.id, "backup.now", "create", "/nodes/localhost/vzdump", args, source=source,
                             requested_by=requested_by, vmid=body.vmid)
