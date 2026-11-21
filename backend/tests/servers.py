"""Small FTP/FTPS and SSH (SFTP + SCP + shell) servers for the backup tests."""

from __future__ import annotations

import asyncio
import subprocess
import tempfile
from pathlib import Path


def free_port() -> int:
    import socket

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def self_signed_cert(dirpath: Path) -> Path:
    pem = dirpath / "cert.pem"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(pem),
                    "-out", str(pem), "-days", "1", "-subj", "/CN=127.0.0.1"], check=True, capture_output=True)
    return pem


class FtpServer:
    """pyftpdlib in a child process (pyOpenSSL and Python's ssl must not share a process)."""

    def __init__(self, tls: bool = False) -> None:
        import sys
        import time

        self.root = Path(tempfile.mkdtemp(prefix="ftp-root-"))
        self.port = free_port()
        self.proc = subprocess.Popen([sys.executable, __file__, str(self.root), str(self.port), "1" if tls else "0"])
        import socket

        for _ in range(100):
            try:
                socket.create_connection(("127.0.0.1", self.port), timeout=0.2).close()
                break
            except OSError:
                time.sleep(0.1)

    def stop(self) -> None:
        self.proc.terminate()
        self.proc.wait(5)


def _serve_ftp(root: str, port: int, tls: bool) -> None:
    from pyftpdlib.authorizers import DummyAuthorizer
    from pyftpdlib.handlers import FTPHandler, TLS_FTPHandler
    from pyftpdlib.servers import FTPServer

    auth = DummyAuthorizer()
    auth.add_user("backup", "s3cret-pass", root, perm="elradfmwMT")
    handler = TLS_FTPHandler if tls else FTPHandler
    if tls:
        handler.certfile = str(self_signed_cert(Path(tempfile.mkdtemp())))
        handler.tls_control_required = True
        handler.tls_data_required = True
    handler.authorizer = auth
    base = 61000 if tls else 60000
    handler.passive_ports = range(base, base + 200)
    FTPServer(("127.0.0.1", port), handler).serve_forever()


class SshServer:
    """asyncssh server with SFTP, SCP and a minimal shell, rooted in a temp dir."""

    def __init__(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="ssh-root-"))
        self.port = free_port()
        self.server = None

    async def start(self) -> None:
        import asyncssh

        root = self.root

        class Server(asyncssh.SSHServer):
            def begin_auth(self, username):
                return True

            def password_auth_supported(self):
                return True

            def validate_password(self, username, password):
                return username == "backup" and password == "s3cret-pass"

        async def process(proc: asyncssh.SSHServerProcess) -> None:
            # Run the command in the chroot-like directory (tests only).
            p = await asyncio.create_subprocess_shell(proc.command or "true", cwd=str(root),
                                                      stdout=asyncio.subprocess.PIPE,
                                                      stderr=asyncio.subprocess.PIPE)
            out, err = await p.communicate()
            proc.stdout.write(out.decode())
            proc.stderr.write(err.decode())
            proc.exit(p.returncode or 0)

        class Sftp(asyncssh.SFTPServer):
            def __init__(self, chan):
                super().__init__(chan, chroot=str(root).encode())

        key = asyncssh.generate_private_key("ssh-ed25519")
        self.host_key = key
        self.server = await asyncssh.create_server(
            Server, "127.0.0.1", self.port, server_host_keys=[key], sftp_factory=Sftp, allow_scp=True,
            process_factory=process)

    def stop(self) -> None:
        if self.server:
            self.server.close()


if __name__ == "__main__":
    import logging
    import sys

    logging.disable(logging.CRITICAL)
    _serve_ftp(sys.argv[1], int(sys.argv[2]), sys.argv[3] == "1")
