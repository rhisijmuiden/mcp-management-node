"""Remote storage for backups: FTP, FTPS (explicit and implicit), SFTP and SCP."""

from __future__ import annotations

import asyncio
import ftplib
import posixpath
import shlex
import socket
import ssl
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import asyncssh


@dataclass
class TargetConfig:
    protocol: str
    host: str
    port: int
    username: str
    password: str | None = None
    private_key: str | None = None
    remote_path: str = "."
    options: dict[str, Any] = field(default_factory=dict)


class TransportError(Exception):
    pass


class Transport(ABC):
    def __init__(self, cfg: TargetConfig) -> None:
        self.cfg = cfg

    def _remote(self, name: str) -> str:
        if "/" in name or name in (".", ".."):
            raise TransportError(f"invalid remote name: {name}")
        return posixpath.join(self.cfg.remote_path or ".", name)

    @abstractmethod
    async def test(self) -> dict[str, Any]: ...

    @abstractmethod
    async def upload(self, local: Path, name: str) -> None: ...

    @abstractmethod
    async def download(self, name: str, local: Path) -> None: ...

    @abstractmethod
    async def list(self) -> list[str]: ...

    @abstractmethod
    async def delete(self, name: str) -> None: ...


# ---------------------------------------------------------------- FTP / FTPS
class _ImplicitFTP_TLS(ftplib.FTP_TLS):
    """FTPS with implicit TLS (usually port 990)."""

    def __init__(self, *a: Any, **kw: Any) -> None:
        super().__init__(*a, **kw)
        self._sock: ssl.SSLSocket | None = None

    @property
    def sock(self):  # type: ignore[override]
        return self._sock

    @sock.setter
    def sock(self, value):  # type: ignore[override]
        if value is not None and not isinstance(value, ssl.SSLSocket):
            value = self.context.wrap_socket(value, server_hostname=self.host)
        self._sock = value


class FtpTransport(Transport):
    def _connect(self) -> ftplib.FTP:
        o = self.cfg.options
        timeout = int(o.get("timeout", 30))
        if self.cfg.protocol == "ftps":
            ctx = ssl.create_default_context()
            ctx.minimum_version = ssl.TLSVersion.TLSv1_2
            if not o.get("verify_tls", True):
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
            cls = _ImplicitFTP_TLS if o.get("ftps_implicit") else ftplib.FTP_TLS
            ftp: ftplib.FTP = cls(context=ctx, timeout=timeout)
        else:
            ftp = ftplib.FTP(timeout=timeout)
        ftp.connect(self.cfg.host, self.cfg.port)
        if isinstance(ftp, ftplib.FTP_TLS) and not isinstance(ftp, _ImplicitFTP_TLS):
            ftp.auth()
        ftp.login(self.cfg.username, self.cfg.password or "")
        if isinstance(ftp, ftplib.FTP_TLS):
            ftp.prot_p()
        ftp.set_pasv(bool(o.get("passive", True)))
        return ftp

    def _ensure_dir(self, ftp: ftplib.FTP) -> None:
        path = self.cfg.remote_path or "."
        if path in (".", ""):
            return
        home = ftp.pwd()
        try:
            ftp.cwd(path)
            ftp.cwd(home)
            return
        except ftplib.error_perm:
            pass
        cur = "/" if path.startswith("/") else ""
        for part in [p for p in path.split("/") if p]:
            cur = posixpath.join(cur, part) if cur else part
            try:
                ftp.mkd(cur)
            except ftplib.error_perm:
                pass

    def _run(self, fn):
        def wrapper():
            ftp = self._connect()
            try:
                return fn(ftp)
            finally:
                try:
                    ftp.quit()
                except Exception:  # noqa: BLE001
                    ftp.close()

        return asyncio.to_thread(wrapper)

    async def test(self) -> dict[str, Any]:
        def fn(ftp: ftplib.FTP):
            self._ensure_dir(ftp)
            probe = self._remote(".mmn-write-test")
            import io

            ftp.storbinary(f"STOR {probe}", io.BytesIO(b"ok"))
            ftp.delete(probe)
            return {"welcome": ftp.getwelcome()[:200], "tls": isinstance(ftp, ftplib.FTP_TLS)}

        return await self._run(fn)

    async def upload(self, local: Path, name: str) -> None:
        def fn(ftp: ftplib.FTP):
            self._ensure_dir(ftp)
            tmp = self._remote(name + ".part")
            with local.open("rb") as f:
                ftp.storbinary(f"STOR {tmp}", f)
            ftp.rename(tmp, self._remote(name))

        await self._run(fn)

    async def download(self, name: str, local: Path) -> None:
        def fn(ftp: ftplib.FTP):
            with local.open("wb") as f:
                ftp.retrbinary(f"RETR {self._remote(name)}", f.write)

        await self._run(fn)

    async def list(self) -> list[str]:
        def fn(ftp: ftplib.FTP):
            try:
                names = ftp.nlst(self.cfg.remote_path or ".")
            except ftplib.error_perm as e:
                if str(e).startswith("550"):
                    return []
                raise
            return sorted(posixpath.basename(n) for n in names)

        return await self._run(fn)

    async def delete(self, name: str) -> None:
        await self._run(lambda ftp: ftp.delete(self._remote(name)))


# ---------------------------------------------------------------- SSH (SFTP / SCP)
class SshTransport(Transport):
    """SFTP, or SCP for the file copy itself (listing and delete then go over the SSH channel)."""

    def _known_hosts(self):
        key = self.cfg.options.get("host_key")
        if not key:
            return None  # trust on first use; the key is stored after a successful test
        host = self.cfg.host if self.cfg.port == 22 else f"[{self.cfg.host}]:{self.cfg.port}"
        return asyncssh.import_known_hosts(f"{host} {key}\n")

    async def _connect(self, accept_unknown: bool = False) -> asyncssh.SSHClientConnection:
        known = self._known_hosts()
        if known is None and not accept_unknown:
            raise TransportError("host key unknown: test the connection first to trust the server key")
        kwargs: dict[str, Any] = {
            "host": self.cfg.host, "port": self.cfg.port, "username": self.cfg.username,
            "known_hosts": known, "connect_timeout": int(self.cfg.options.get("timeout", 30)),
        }
        if self.cfg.private_key:
            kwargs["client_keys"] = [asyncssh.import_private_key(self.cfg.private_key,
                                                                 self.cfg.options.get("key_passphrase"))]
            kwargs["password"] = self.cfg.password or None
        else:
            kwargs["client_keys"] = None
            kwargs["password"] = self.cfg.password or ""
        try:
            return await asyncssh.connect(**kwargs)
        except asyncssh.HostKeyNotVerifiable as e:
            raise TransportError("host key mismatch: the server key changed") from e
        except (OSError, asyncssh.Error) as e:
            raise TransportError(str(e) or type(e).__name__) from e

    async def _mkdirs(self, conn: asyncssh.SSHClientConnection) -> None:
        path = self.cfg.remote_path or "."
        if path in (".", ""):
            return
        if self.cfg.protocol == "sftp":
            async with conn.start_sftp_client() as sftp:
                await sftp.makedirs(path, exist_ok=True)
        else:
            await conn.run(f"mkdir -p -- {shlex.quote(path)}", check=True)

    async def test(self) -> dict[str, Any]:
        conn = await self._connect(accept_unknown=True)
        async with conn:
            server_key = conn.get_server_host_key()
            await self._mkdirs(conn)
            probe = self._remote(".mmn-write-test")
            if self.cfg.protocol == "sftp":
                async with conn.start_sftp_client() as sftp:
                    async with sftp.open(probe, "wb") as f:
                        await f.write(b"ok")
                    await sftp.remove(probe)
            else:
                await conn.run(f"printf ok > {shlex.quote(probe)} && rm -f -- {shlex.quote(probe)}", check=True)
            key_line = server_key.export_public_key().decode().strip() if server_key else None
            fp = server_key.get_fingerprint("sha256") if server_key else None
            return {"host_key": key_line, "fingerprint": fp}

    async def upload(self, local: Path, name: str) -> None:
        async with await self._connect() as conn:
            await self._mkdirs(conn)
            tmp, final = self._remote(name + ".part"), self._remote(name)
            if self.cfg.protocol == "sftp":
                async with conn.start_sftp_client() as sftp:
                    await sftp.put(str(local), tmp)
                    await _sftp_replace(sftp, tmp, final)
            else:
                await asyncssh.scp(str(local), (conn, tmp))
                await conn.run(f"mv -f -- {shlex.quote(tmp)} {shlex.quote(final)}", check=True)

    async def download(self, name: str, local: Path) -> None:
        async with await self._connect() as conn:
            if self.cfg.protocol == "sftp":
                async with conn.start_sftp_client() as sftp:
                    await sftp.get(self._remote(name), str(local))
            else:
                await asyncssh.scp((conn, self._remote(name)), str(local))

    async def list(self) -> list[str]:
        async with await self._connect() as conn:
            path = self.cfg.remote_path or "."
            if self.cfg.protocol == "sftp":
                async with conn.start_sftp_client() as sftp:
                    try:
                        return sorted(n for n in await sftp.listdir(path) if n not in (".", ".."))
                    except asyncssh.SFTPNoSuchFile:
                        return []
            res = await conn.run(f"ls -1A -- {shlex.quote(path)} 2>/dev/null || true", check=False)
            return sorted(line for line in str(res.stdout or "").splitlines() if line)

    async def delete(self, name: str) -> None:
        async with await self._connect() as conn:
            if self.cfg.protocol == "sftp":
                async with conn.start_sftp_client() as sftp:
                    await sftp.remove(self._remote(name))
            else:
                await conn.run(f"rm -f -- {shlex.quote(self._remote(name))}", check=True)


async def _sftp_replace(sftp: asyncssh.SFTPClient, tmp: str, final: str) -> None:
    try:
        await sftp.posix_rename(tmp, final)
    except (asyncssh.SFTPOpUnsupported, asyncssh.SFTPError):
        try:
            await sftp.remove(final)
        except asyncssh.SFTPNoSuchFile:
            pass
        await sftp.rename(tmp, final)


DEFAULT_PORTS = {"ftp": 21, "ftps": 21, "sftp": 22, "scp": 22}


def make_transport(cfg: TargetConfig) -> Transport:
    if cfg.protocol in ("ftp", "ftps"):
        return FtpTransport(cfg)
    if cfg.protocol in ("sftp", "scp"):
        return SshTransport(cfg)
    raise TransportError(f"unknown protocol {cfg.protocol}")


def friendly_error(e: BaseException) -> str:
    if isinstance(e, socket.gaierror):
        return "host not found"
    if isinstance(e, TimeoutError | socket.timeout):
        return "connection timed out"
    if isinstance(e, ConnectionRefusedError):
        return "connection refused"
    if isinstance(e, ssl.SSLCertVerificationError):
        return f"TLS certificate not trusted: {e.verify_message}"
    if isinstance(e, ftplib.error_perm):
        return f"FTP: {e}"
    return f"{type(e).__name__}: {e}" if str(e) else type(e).__name__
