"""Command line tool: ``mmn <command>``."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import secrets
import sys
from pathlib import Path


def _alembic_cfg():
    from alembic.config import Config

    here = Path(__file__).resolve().parent
    cfg = Config()
    cfg.set_main_option("script_location", str(here / "migrations"))
    return cfg


def migrate(_: argparse.Namespace) -> int:
    from alembic import command

    command.upgrade(_alembic_cfg(), "head")
    return 0


def setup_token(args: argparse.Namespace) -> int:
    from mmn.config import get_settings

    f = get_settings().data_dir / "setup-token"
    if f.exists() and not args.new:
        print(f.read_text().strip())
        return 0
    f.parent.mkdir(parents=True, exist_ok=True)
    tok = "-".join(secrets.token_hex(3) for _ in range(4))
    f.write_text(tok + "\n")
    f.chmod(0o600)
    print(tok)
    return 0


def create_admin(args: argparse.Namespace) -> int:
    from sqlalchemy import select

    from mmn.db import dispose_engine, sessionmaker
    from mmn.models import Role, User
    from mmn.security import MIN_PASSWORD_LENGTH, hash_password

    password = args.password or getpass.getpass("Password: ")
    if len(password) < MIN_PASSWORD_LENGTH:
        print(f"password must be at least {MIN_PASSWORD_LENGTH} characters", file=sys.stderr)
        return 1

    async def run() -> int:
        async with sessionmaker()() as db:
            existing = (await db.execute(select(User).where(User.username == args.username.lower()))).scalar_one_or_none()
            if existing:
                existing.password_hash = hash_password(password)
                existing.role = Role.admin
                existing.is_active = True
                print(f"updated {existing.username}")
            else:
                db.add(User(username=args.username.lower(), email=args.email, role=Role.admin,
                            password_hash=hash_password(password)))
                print(f"created {args.username.lower()}")
            await db.commit()
        await dispose_engine()
        return 0

    return asyncio.run(run())


def backup(args: argparse.Namespace) -> int:
    from mmn.db import dispose_engine, sessionmaker
    from mmn.models import BackupRun
    from mmn.services.backup.engine import run_backup

    async def run() -> int:
        run_id = await run_backup(args.target, args.kind, trigger="cli")
        async with sessionmaker()() as db:
            r = await db.get(BackupRun, run_id)
            print(f"{r.kind} backup {r.status}: {r.backup_id or ''} {r.error or ''}".strip())
            ok = r.status == "success"
        await dispose_engine()
        return 0 if ok else 1

    return asyncio.run(run())


def restore(args: argparse.Namespace) -> int:
    from mmn.db import dispose_engine
    from mmn.services.backup.engine import restore_backup

    async def run() -> int:
        res = await restore_backup(args.target, args.backup_id, username="cli", source="cli")
        print(f"restored {res['restored']} (chain: {', '.join(res['chain'])})")
        await dispose_engine()
        return 0

    return asyncio.run(run())


def serve(_: argparse.Namespace) -> int:
    import uvicorn

    from mmn.config import get_settings

    s = get_settings()
    uvicorn.run("mmn.main:app", host=s.listen_host, port=s.listen_port, proxy_headers=True,
                forwarded_allow_ips="127.0.0.1,::1", log_level=s.log_level, ws="websockets-sansio")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="mmn", description="MCP Management Node")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("migrate", help="upgrade the database schema").set_defaults(fn=migrate)
    sub.add_parser("serve", help="run the web app").set_defaults(fn=serve)
    st = sub.add_parser("setup-token", help="show (or create) the one-time setup token")
    st.add_argument("--new", action="store_true")
    st.set_defaults(fn=setup_token)
    ca = sub.add_parser("create-admin", help="create an admin or reset its password")
    ca.add_argument("username")
    ca.add_argument("--email")
    ca.add_argument("--password")
    ca.set_defaults(fn=create_admin)
    b = sub.add_parser("backup", help="run a backup now")
    b.add_argument("target", help="target id")
    b.add_argument("--kind", default="auto", choices=["auto", "full", "incremental"])
    b.set_defaults(fn=backup)
    r = sub.add_parser("restore", help="restore a backup from a remote target")
    r.add_argument("target")
    r.add_argument("backup_id")
    r.set_defaults(fn=restore)
    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
