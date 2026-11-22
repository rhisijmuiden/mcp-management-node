"""Full and incremental backups of the app (database + data files) to remote targets, and restore.

How it works
------------
* The database is dumped with ``pg_dump --format=directory --compress=0``: one file per table.
  Together with the files from the data directory this gives a set of files with a SHA-256 manifest.
* A **full** backup stores every file. An **incremental** backup stores only files whose hash
  changed since the previous backup of the same chain on that target, plus a list of deleted
  files. Tables that did not change are therefore not stored again.
* Each backup is one ``.tar.gz`` archive (optionally AES-256-GCM encrypted, ``.enc``) plus a
  small JSON sidecar. The sidecar describes the chain, so a fresh install can list and restore
  backups straight from the remote location.
* Restore downloads the chain (full + incrementals up to the chosen point), verifies every
  checksum, rebuilds the file set and loads it into PostgreSQL. A safety dump is taken first and
  put back automatically if the restore fails.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import os
import shutil
import tarfile
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession

from mmn.config import get_settings
from mmn.db import dispose_engine, sessionmaker
from mmn.i18n import t
from mmn.models import BackupRun, BackupTarget, new_id, utcnow
from mmn.security import decrypt_secret, reset_secret_cache
from mmn.services import events
from mmn.services.backup.crypto import decrypt_file, encrypt_file
from mmn.services.backup.transports import TargetConfig, friendly_error, make_transport
from mmn.services.settings_store import get_setting
from mmn.version import get_build_info

log = logging.getLogger("mmn.backup")

FORMAT_VERSION = 1
SIDE_SUFFIX = ".json"
_lock = asyncio.Lock()
maintenance: dict[str, Any] = {"active": False, "reason": None}


class BackupError(Exception):
    pass


def target_config(target: BackupTarget) -> TargetConfig:
    return TargetConfig(
        protocol=target.protocol.value if hasattr(target.protocol, "value") else str(target.protocol),
        host=target.host, port=target.port, username=target.username,
        password=decrypt_secret(target.password_enc), private_key=decrypt_secret(target.private_key_enc),
        remote_path=target.remote_path or ".", options=dict(target.options or {}),
    )


# ------------------------------------------------------------------ helpers
def _pg_env() -> tuple[list[str], dict[str, str]]:
    url = make_url(get_settings().sync_database_url)
    env = dict(os.environ)
    if url.password:
        env["PGPASSWORD"] = str(url.password)
    args = ["-h", url.host or "127.0.0.1", "-p", str(url.port or 5432), "-U", url.username or "mmn"]
    return args + ["-d", url.database or "mmn"], env


async def _run(cmd: list[str], env: dict[str, str] | None = None) -> str:
    proc = await asyncio.create_subprocess_exec(*cmd, env=env, stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.PIPE)
    out, err = await proc.communicate()
    if proc.returncode != 0:
        raise BackupError(f"{Path(cmd[0]).name} failed: {err.decode(errors='replace').strip()[-800:]}")
    return out.decode(errors="replace")


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def _manifest(root: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for p in sorted(root.rglob("*")):
        if p.is_file():
            out[p.relative_to(root).as_posix()] = _sha256(p)
    return out


def _safe_member(name: str) -> bool:
    return not (name.startswith("/") or ".." in Path(name).parts)


def human_size(n: int | None) -> str:
    if n is None:
        return "-"
    size = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{n} B"


async def _stage(stage: Path) -> None:
    """Collect everything that belongs in a backup into ``stage``."""
    s = get_settings()
    db_dir = stage / "db"
    args, env = _pg_env()
    await _run(["pg_dump", "--format=directory", "--compress=0", "--no-owner", "--no-privileges",
                "--file", str(db_dir), *args], env)
    # toc.dat contains a timestamp; that is fine, it is small.
    files = stage / "files"
    files.mkdir(parents=True)
    if s.secret_key_file.exists():
        shutil.copy2(s.secret_key_file, files / "secret.key")
    if s.files_dir.exists():
        shutil.copytree(s.files_dir, files / "data", dirs_exist_ok=True)


def _publish(run: BackupRun, step: str, **extra: Any) -> None:
    events.publish("backup.progress", {"run_id": run.id, "kind": run.kind, "step": step,
                                       "target_id": run.target_id, **extra})


def _log(run: BackupRun, msg: str) -> None:
    run.log = [*(run.log or []), {"at": utcnow().isoformat(), "msg": msg}]
    _publish(run, msg)


# ------------------------------------------------------------------ backup
async def run_backup(target_id: str, kind: str = "auto", trigger: str = "manual") -> str:
    """Run a backup and return the run id. ``kind`` is ``full``, ``incremental`` or ``auto``."""
    if maintenance["active"]:
        raise BackupError("a restore is in progress")
    async with _lock:
        async with sessionmaker()() as db:
            target = await db.get(BackupTarget, target_id)
            if target is None:
                raise BackupError("target not found")
            prev = (await db.execute(
                select(BackupRun).where(BackupRun.target_id == target_id, BackupRun.status == "success",
                                        BackupRun.kind.in_(["full", "incremental"]))
                .order_by(BackupRun.started_at.desc()).limit(1))).scalar_one_or_none()
            if kind == "auto":
                kind = "incremental" if prev else "full"
            if kind == "incremental" and prev is None:
                kind = "full"
            run = BackupRun(target_id=target_id, kind=kind, trigger=trigger, status="running")
            db.add(run)
            await db.commit()
            try:
                await _do_backup(db, target, run, prev if kind == "incremental" else None)
                run.status = "success"
            except Exception as e:  # noqa: BLE001
                log.exception("Backup %s failed", run.id)
                run.status = "failed"
                run.error = friendly_error(e) if not isinstance(e, BackupError) else str(e)
            run.finished_at = utcnow()
            _publish(run, "finished", status=run.status, error=run.error)
            await db.commit()
            await _notify(db, target, run)
            return run.id


async def _do_backup(db: AsyncSession, target: BackupTarget, run: BackupRun, parent: BackupRun | None) -> None:
    s = get_settings()
    s.run_dir.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="backup-", dir=s.run_dir))
    try:
        stage = work / "stage"
        stage.mkdir()
        _log(run, "dumping database")
        await _stage(stage)
        manifest = await asyncio.to_thread(_manifest, stage)
        prev_manifest: dict[str, str] = (parent.manifest or {}).get("files", {}) if parent else {}
        changed = [p for p, h in manifest.items() if prev_manifest.get(p) != h]
        deleted = sorted(set(prev_manifest) - set(manifest))
        now = datetime.now(UTC)
        backup_id = f"mmn-{now.strftime('%Y%m%dT%H%M%SZ')}-{'full' if parent is None else 'incr'}-{run.id[:6]}"
        chain_id = parent.chain_id if parent else backup_id
        meta: dict[str, Any] = {
            "format": FORMAT_VERSION, "id": backup_id, "kind": run.kind, "chain_id": chain_id,
            "parent": parent.backup_id if parent else None, "created_at": now.isoformat(),
            "app_version": get_build_info().version, "files_changed": len(changed),
            "files": manifest, "deleted": deleted,
        }
        _log(run, f"packing {len(changed)} of {len(manifest)} files")
        archive = work / f"{backup_id}.tar.gz"

        def pack() -> None:
            with tarfile.open(archive, "w:gz", compresslevel=6) as tar:
                raw = json.dumps(meta, indent=1).encode()
                info = tarfile.TarInfo("mmn-backup.json")
                info.size, info.mtime = len(raw), int(now.timestamp())
                tar.addfile(info, io.BytesIO(raw))
                for rel in changed:
                    tar.add(stage / rel, arcname=f"data/{rel}", recursive=False)

        await asyncio.to_thread(pack)
        passphrase = decrypt_secret(target.passphrase_enc)
        upload_file = archive
        if passphrase:
            _log(run, "encrypting")
            upload_file = work / f"{archive.name}.enc"
            await asyncio.to_thread(encrypt_file, archive, upload_file, passphrase)
        sidecar = {k: v for k, v in meta.items()}
        sidecar.update({"archive": upload_file.name, "archive_sha256": await asyncio.to_thread(_sha256, upload_file),
                        "size": upload_file.stat().st_size, "encrypted": bool(passphrase)})
        side_path = work / f"{backup_id}{SIDE_SUFFIX}"
        side_path.write_text(json.dumps(sidecar, indent=1))

        transport = make_transport(target_config(target))
        _log(run, f"uploading {human_size(sidecar['size'])} to {target.name}")
        await transport.upload(upload_file, upload_file.name)
        await transport.upload(side_path, side_path.name)  # written last: marks the backup complete
        run.backup_id, run.chain_id = backup_id, chain_id
        run.parent_backup_id = parent.backup_id if parent else None
        run.manifest = {"files": manifest}
        run.size_bytes, run.files_changed = sidecar["size"], len(changed)
        if parent is None:
            await _apply_retention(target, transport, run)
    finally:
        shutil.rmtree(work, ignore_errors=True)


async def _apply_retention(target: BackupTarget, transport, run: BackupRun) -> None:
    keep = max(1, int(target.keep_full or 1))
    try:
        metas = await list_remote_backups_with(transport)
    except Exception as e:  # noqa: BLE001
        _log(run, f"retention skipped: {friendly_error(e)}")
        return
    chains: dict[str, str] = {}
    for m in metas:
        if m["kind"] == "full":
            chains[m["chain_id"]] = m["created_at"]
    old = sorted(chains, key=lambda c: chains[c], reverse=True)[keep:]
    for m in metas:
        if m["chain_id"] in old:
            for name in (m["archive"], f"{m['id']}{SIDE_SUFFIX}"):
                try:
                    await transport.delete(name)
                except Exception as e:  # noqa: BLE001
                    _log(run, f"could not delete {name}: {friendly_error(e)}")
    if old:
        _log(run, f"removed {len(old)} old backup chain(s)")


async def _notify(db: AsyncSession, target: BackupTarget, run: BackupRun) -> None:
    from mmn.services.mailer import MailContent, try_send_mail

    notif = await get_setting(db, "notifications")
    general = await get_setting(db, "general")
    lang = general.get("default_language", "en")
    to = notif.get("backup_failure_to") if run.status == "failed" else notif.get("backup_success_to")
    if not to:
        return
    key = "backup.failed" if run.status == "failed" else "backup.ok"
    when = (run.finished_at or utcnow()).strftime("%Y-%m-%d %H:%M UTC")
    paragraphs = [t(lang, f"{key}.body", kind=run.kind, target=target.name, time=when,
                    size=human_size(run.size_bytes))]
    if run.error:
        paragraphs.append(run.error)
    await try_send_mail(db, to, MailContent(subject=t(lang, f"{key}.subject", target=target.name),
                                            title=t(lang, f"{key}.title"), paragraphs=paragraphs, muted=[]), lang)


# ------------------------------------------------------------------ listing
async def list_remote_backups_with(transport) -> list[dict[str, Any]]:
    names = await transport.list()
    sidecars = [n for n in names if n.startswith("mmn-") and n.endswith(SIDE_SUFFIX)]
    out: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(dir=get_settings().run_dir) as tmp:
        for n in sidecars:
            p = Path(tmp) / n
            try:
                await transport.download(n, p)
                meta = json.loads(p.read_text())
            except Exception:  # noqa: BLE001 - skip unreadable sidecars
                continue
            if meta.get("format") != FORMAT_VERSION or meta.get("archive") not in names:
                continue
            out.append(meta)
    out.sort(key=lambda m: m["created_at"], reverse=True)
    return out


async def list_remote_backups(target: BackupTarget) -> list[dict[str, Any]]:
    get_settings().run_dir.mkdir(parents=True, exist_ok=True)
    metas = await list_remote_backups_with(make_transport(target_config(target)))
    by_id = {m["id"]: m for m in metas}
    result = []
    for m in metas:
        # A backup is restorable if its whole chain is present.
        ok, cur = True, m
        while cur.get("parent"):
            cur = by_id.get(cur["parent"])
            if cur is None:
                ok = False
                break
        result.append({k: v for k, v in m.items() if k not in ("files", "deleted")} | {"restorable": ok})
    return result


# ------------------------------------------------------------------ restore
async def restore_backup(target_id: str, backup_id: str, *, user_id: str | None = None,
                         username: str | None = None, source: str = "ui") -> dict[str, Any]:
    if _lock.locked():
        raise BackupError("another backup or restore is running")
    async with _lock:
        async with sessionmaker()() as db:
            target = await db.get(BackupTarget, target_id)
            if target is None:
                raise BackupError("target not found")
            cfg = target_config(target)
            passphrase = decrypt_secret(target.passphrase_enc)
            target_name = target.name
        s = get_settings()
        s.run_dir.mkdir(parents=True, exist_ok=True)
        work = Path(tempfile.mkdtemp(prefix="restore-", dir=s.run_dir))
        transport = make_transport(cfg)
        maintenance.update(active=True, reason="restore")
        events.publish("maintenance", {"active": True, "reason": "restore"})
        try:
            events.publish("restore.progress", {"step": "downloading"})
            metas = {m["id"]: m for m in await list_remote_backups_with(transport)}
            if backup_id not in metas:
                raise BackupError("backup not found on the remote location")
            chain = [metas[backup_id]]
            while chain[0].get("parent"):
                parent = metas.get(chain[0]["parent"])
                if parent is None:
                    raise BackupError(f"chain is incomplete: {chain[0]['parent']} is missing")
                chain.insert(0, parent)
            state = work / "state"
            state.mkdir()
            for m in chain:
                await _apply_archive(transport, m, work, state, passphrase)
            final = chain[-1]
            actual = await asyncio.to_thread(_manifest, state)
            if actual != final["files"]:
                raise BackupError("verification failed: restored files do not match the backup manifest")
            events.publish("restore.progress", {"step": "loading database"})
            await _load_database(state / "db", work)
            sk = state / "files" / "secret.key"
            if sk.exists():
                shutil.copy2(sk, s.secret_key_file)
                os.chmod(s.secret_key_file, 0o600)
                reset_secret_cache()
            data = state / "files" / "data"
            if data.exists():
                if s.files_dir.exists():
                    shutil.rmtree(s.files_dir)
                shutil.copytree(data, s.files_dir)
            result = {"restored": backup_id, "chain": [m["id"] for m in chain], "created_at": final["created_at"]}
            async with sessionmaker()() as db:
                db.add(BackupRun(id=new_id(), target_id=target_id if await db.get(BackupTarget, target_id) else None,
                                 kind="restore", status="success", trigger=source, backup_id=backup_id,
                                 chain_id=final["chain_id"], finished_at=utcnow(),
                                 log=[{"at": utcnow().isoformat(), "msg": f"restored from {target_name}"}]))
                from mmn.services.audit import audit

                await audit(db, "backup.restore", source=source, target=backup_id, success=True,
                            by=username, by_id=user_id)
                await db.commit()
            return result
        finally:
            shutil.rmtree(work, ignore_errors=True)
            maintenance.update(active=False, reason=None)
            events.publish("maintenance", {"active": False})


async def _apply_archive(transport, meta: dict[str, Any], work: Path, state: Path, passphrase: str | None) -> None:
    local = work / meta["archive"]
    await transport.download(meta["archive"], local)
    if await asyncio.to_thread(_sha256, local) != meta["archive_sha256"]:
        raise BackupError(f"checksum mismatch for {meta['archive']}")
    if meta.get("encrypted"):
        if not passphrase:
            raise BackupError("this backup is encrypted: set the encryption passphrase on the target")
        plain = work / (meta["archive"].removesuffix(".enc"))
        await asyncio.to_thread(decrypt_file, local, plain, passphrase)
        local.unlink()
        local = plain

    def extract() -> None:
        with tarfile.open(local, "r:gz") as tar:
            for member in tar.getmembers():
                if not member.name.startswith("data/") or not member.isfile() or not _safe_member(member.name):
                    continue
                rel = member.name[len("data/"):]
                dest = state / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                src = tar.extractfile(member)
                assert src is not None
                with dest.open("wb") as f:
                    shutil.copyfileobj(src, f)
        for rel in meta.get("deleted", []):
            if _safe_member(rel):
                (state / rel).unlink(missing_ok=True)

    await asyncio.to_thread(extract)
    local.unlink(missing_ok=True)


async def _load_database(dump_dir: Path, work: Path) -> None:
    args, env = _pg_env()
    safety = work / "pre-restore"
    await _run(["pg_dump", "--format=directory", "--no-owner", "--no-privileges", "--file", str(safety), *args], env)
    await dispose_engine()
    try:
        await _reset_schema()
        await _run(["pg_restore", "--no-owner", "--no-privileges", "--single-transaction", "--exit-on-error",
                    *args, str(dump_dir)], env)
    except Exception:
        log.exception("Restore failed, putting the previous database back")
        await dispose_engine()
        await _reset_schema()
        await _run(["pg_restore", "--no-owner", "--no-privileges", "--single-transaction", *args, str(safety)], env)
        raise
    finally:
        await dispose_engine()


async def _reset_schema() -> None:
    from mmn.db import get_engine

    engine = get_engine()
    async with engine.begin() as conn:
        # Stop other sessions of this database user so the schema can be dropped.
        await conn.execute(text(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = current_database() AND pid <> pg_backend_pid() AND usename = current_user"))
        await conn.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
        await conn.execute(text("CREATE SCHEMA public"))
    await dispose_engine()
