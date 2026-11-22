"""Backup target management shared by REST and MCP."""

from __future__ import annotations

import asyncio
import re
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mmn.models import BackupProtocol, BackupRun, BackupTarget
from mmn.security import encrypt_secret
from mmn.services.backup.transports import DEFAULT_PORTS

HOST_RE = re.compile(r"^[A-Za-z0-9.\-:\[\]]{1,255}$")
_tasks: set[asyncio.Task] = set()


def spawn(coro) -> asyncio.Task:
    task = asyncio.create_task(coro)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return task


class TargetIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    protocol: Literal["ftp", "ftps", "sftp", "scp"]
    host: str
    port: int | None = Field(default=None, ge=1, le=65535)
    username: str = Field(min_length=1, max_length=128)
    password: str | None = Field(default=None, max_length=1024)  # None keeps, "" clears
    private_key: str | None = Field(default=None, max_length=20000)
    key_passphrase: str | None = Field(default=None, max_length=1024)
    encryption_passphrase: str | None = Field(default=None, max_length=1024)
    remote_path: str = Field(default=".", max_length=500)
    ftps_implicit: bool = False
    verify_tls: bool = True
    passive: bool = True
    enabled: bool = True
    full_cron: str | None = Field(default="0 3 * * 0", max_length=64)
    incremental_cron: str | None = Field(default="0 3 * * 1-6", max_length=64)
    keep_full: int = Field(default=4, ge=1, le=100)

    @field_validator("host")
    @classmethod
    def _host(cls, v: str) -> str:
        v = v.strip()
        if not HOST_RE.match(v):
            raise ValueError("invalid host")
        return v

    @field_validator("full_cron", "incremental_cron")
    @classmethod
    def _cron(cls, v: str | None) -> str | None:
        if v is None or not v.strip():
            return None
        from mmn.services.scheduler import validate_cron

        validate_cron(v.strip())
        return v.strip()

    @field_validator("remote_path")
    @classmethod
    def _path(cls, v: str) -> str:
        v = v.strip() or "."
        if "\x00" in v or "\n" in v:
            raise ValueError("invalid path")
        return v.rstrip("/") or "/"


def target_out(t: BackupTarget) -> dict[str, Any]:
    o = t.options or {}
    return {
        "id": t.id, "name": t.name, "protocol": t.protocol.value, "host": t.host, "port": t.port,
        "username": t.username, "remote_path": t.remote_path, "enabled": t.enabled,
        "full_cron": t.full_cron, "incremental_cron": t.incremental_cron, "keep_full": t.keep_full,
        "ftps_implicit": bool(o.get("ftps_implicit")), "verify_tls": o.get("verify_tls", True),
        "passive": o.get("passive", True), "host_key_fingerprint": o.get("host_key_fingerprint"),
        "password_set": bool(t.password_enc), "private_key_set": bool(t.private_key_enc),
        "encrypted": bool(t.passphrase_enc), "created_at": t.created_at,
    }


def apply_target(t: BackupTarget, body: TargetIn, partial_fields: set[str]) -> None:
    host_changed = t.host != body.host or (body.port and t.port != body.port) or t.protocol.value != body.protocol \
        if t.host else True
    t.name, t.protocol, t.host = body.name, BackupProtocol(body.protocol), body.host
    t.port = body.port or DEFAULT_PORTS[body.protocol]
    t.username, t.remote_path, t.enabled = body.username, body.remote_path, body.enabled
    t.full_cron, t.incremental_cron, t.keep_full = body.full_cron, body.incremental_cron, body.keep_full
    opts = dict(t.options or {})
    opts.update(ftps_implicit=body.ftps_implicit, verify_tls=body.verify_tls, passive=body.passive)
    if host_changed:
        opts.pop("host_key", None)
        opts.pop("host_key_fingerprint", None)
    if "key_passphrase" in partial_fields and body.key_passphrase is not None:
        opts["key_passphrase"] = body.key_passphrase or None
    t.options = opts
    if "password" in partial_fields and body.password is not None:
        t.password_enc = encrypt_secret(body.password)
    if "private_key" in partial_fields and body.private_key is not None:
        t.private_key_enc = encrypt_secret(body.private_key.strip() + "\n") if body.private_key.strip() else None
    if "encryption_passphrase" in partial_fields and body.encryption_passphrase is not None:
        t.passphrase_enc = encrypt_secret(body.encryption_passphrase)


async def list_targets(db: AsyncSession) -> list[BackupTarget]:
    return list((await db.execute(select(BackupTarget).order_by(BackupTarget.name))).scalars())


def run_out(r: BackupRun, target_names: dict[str, str] | None = None) -> dict[str, Any]:
    return {
        "id": r.id, "target_id": r.target_id, "target_name": (target_names or {}).get(r.target_id or ""),
        "kind": r.kind, "status": r.status, "trigger": r.trigger, "backup_id": r.backup_id,
        "parent_backup_id": r.parent_backup_id, "chain_id": r.chain_id, "size_bytes": r.size_bytes,
        "files_changed": r.files_changed, "started_at": r.started_at, "finished_at": r.finished_at,
        "error": r.error, "log": r.log,
    }


async def list_runs(db: AsyncSession, limit: int = 50, target_id: str | None = None) -> list[dict[str, Any]]:
    q = select(BackupRun).order_by(BackupRun.started_at.desc()).limit(limit)
    if target_id:
        q = q.where(BackupRun.target_id == target_id)
    runs = (await db.execute(q)).scalars().all()
    names = {t.id: t.name for t in await list_targets(db)}
    return [run_out(r, names) for r in runs]
